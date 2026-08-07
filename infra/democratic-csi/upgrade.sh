#!/usr/bin/env bash
# Apply BOTH democratic-csi releases — iSCSI and NFS — from this one chart.
#
# democratic-csi runs one deployment per backend protocol, so "install the chart"
# means two releases sharing a directory and differing only by overlay:
#
#   democratic-csi-iscsi   values.yaml + values-iscsi.yaml   -> truenas-iscsi
#   democratic-csi-nfs     values.yaml + values-nfs.yaml     -> truenas-nfs
#
# Almost everything that goes wrong here goes wrong OUTSIDE kubernetes — a
# service disabled on the NAS, a dataset that doesn't exist, a node without
# open-iscsi. Helm reports success for all of those and the failure only appears
# later as a PVC stuck Pending or a pod stuck ContainerCreating. So this
# pre-flights each one and refuses to install until they pass.

set -euo pipefail

NAMESPACE="${NAMESPACE:-democratic-csi}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Materialize values.local.yaml from 1Password when missing, same as
# infra/cloudflared. No-ops without `op` — values.local.yaml is still the
# contract, and scripts/secrets.sh publish feeds the claude-workspace pod.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "FAIL: missing ${LOCAL_VALUES}"
  echo "      cp values.local.yaml.example values.local.yaml and add the TrueNAS API key"
  exit 1
fi

# ------------------------------------------------------------------------------
# Pre-flight 1: snapshot controller CRDs
# ------------------------------------------------------------------------------
# A VolumeSnapshotClass is an instance of a CRD owned by infra/snapshot-controller.
# Without it the API server discards our snapshot classes and every snapshot
# taken afterwards silently does nothing. Helm will not complain.
echo "==> Pre-flight: VolumeSnapshot CRDs"
if ! kubectl get volumesnapshotclass >/dev/null 2>&1; then
  echo "FAIL: volumesnapshotclass is not a known resource type."
  echo "      Install infra/snapshot-controller FIRST:"
  echo "        ./infra/snapshot-controller/upgrade.sh"
  exit 1
fi
echo "    [OK] snapshot CRDs registered"

# ------------------------------------------------------------------------------
# Pre-flight 2: node storage clients
# ------------------------------------------------------------------------------
# iSCSI volumes cannot mount on a node without open-iscsi, and NFS volumes cannot
# mount without nfs-common. A node missing either looks perfectly healthy right
# up until a stateful pod is scheduled onto it.
echo "==> Pre-flight: node storage clients"
if [[ -x "${REPO_ROOT}/scripts/k3s/iscsi-prereq.sh" ]]; then
  if ! "${REPO_ROOT}/scripts/k3s/iscsi-prereq.sh" >/dev/null 2>&1; then
    echo "    WARNING: one or more nodes are missing open-iscsi/nfs-common,"
    echo "             or iscsid is not running with OOM protection."
    echo "             Details:  ./scripts/k3s/iscsi-prereq.sh"
    echo "             Fix:      ./scripts/k3s/iscsi-prereq.sh --apply"
    echo ""
    read -rp "    Continue anyway? [y/N] " ok
    [[ "$ok" == "y" || "$ok" == "Y" ]] || exit 1
  else
    echo "    [OK] every node has the storage clients"
  fi
else
  echo "    SKIP: scripts/k3s/iscsi-prereq.sh not executable"
fi

# ------------------------------------------------------------------------------
# Pre-flight 3: the TrueNAS itself
# ------------------------------------------------------------------------------
# Both services ship DISABLED on TrueNAS and both were refused when this
# migration started. A closed port here is the single most likely reason for a
# PVC that never binds.
NAS_HOST="$(awk '/^ *host:/ {print $2; exit}' "${HERE}/values-iscsi.yaml")"
echo "==> Pre-flight: TrueNAS services on ${NAS_HOST}"

probe() {
  local port="$1" label="$2"
  if timeout 5 bash -c "cat < /dev/null > /dev/tcp/${NAS_HOST}/${port}" 2>/dev/null; then
    printf '    [OK]   %-6s %s\n' "$port" "$label"
    return 0
  fi
  printf '    [FAIL] %-6s %s — not listening\n' "$port" "$label"
  return 1
}

NAS_OK=0
probe 443  "API"   || NAS_OK=1
probe 3260 "iSCSI" || NAS_OK=1
probe 2049 "NFS"   || NAS_OK=1

if [[ "$NAS_OK" != "0" ]]; then
  echo ""
  echo "FAIL: enable the missing service(s) in the TrueNAS UI before installing."
  echo "      System Settings -> Services -> iSCSI / NFS (toggle on + 'Start Automatically')"
  exit 1
fi

# ------------------------------------------------------------------------------
# Pre-flight 4: parent datasets exist
# ------------------------------------------------------------------------------
# democratic-csi creates a CHILD dataset per volume; it does not create the
# parents. A missing parent shows up as a PVC that stays Pending with the real
# reason buried in the controller log.
API_KEY="$(awk '/apiKey:/ {gsub(/"/,"",$2); print $2; exit}' "$LOCAL_VALUES" 2>/dev/null || true)"
echo "==> Pre-flight: parent datasets"
if [[ -z "$API_KEY" ]]; then
  echo "    SKIP: could not read apiKey from values.local.yaml"
else
  DS_MISSING=0
  for f in values-iscsi.yaml values-nfs.yaml; do
    while read -r ds; do
      [[ -z "$ds" ]] && continue
      code="$(curl -sk -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${API_KEY}" \
        "https://${NAS_HOST}/api/v2.0/pool/dataset/id/$(printf '%s' "$ds" | sed 's|/|%2F|g')" \
        2>/dev/null || echo 000)"
      if [[ "$code" == "200" ]]; then
        printf '    [OK]   %s\n' "$ds"
      else
        printf '    [FAIL] %s — does not exist (HTTP %s)\n' "$ds" "$code"
        DS_MISSING=1
      fi
    done < <(awk '/DatasetParentName:|datasetParentName:/ {print $2}' "${HERE}/${f}")
  done
  if [[ "$DS_MISSING" != "0" ]]; then
    echo ""
    echo "FAIL: create the missing parent dataset(s) on the NAS first."
    echo "      democratic-csi creates children per volume, never parents."
    exit 1
  fi
fi

# ------------------------------------------------------------------------------
# Install
# ------------------------------------------------------------------------------
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

for driver in iscsi nfs; do
  RELEASE="democratic-csi-${driver}"
  echo ""
  echo "==> helm upgrade --install ${RELEASE} -n ${NAMESPACE}"
  helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
    -f "$VALUES" \
    -f "${HERE}/values-${driver}.yaml" \
    -f "$LOCAL_VALUES"

  echo "==> Waiting for ${RELEASE} rollout"
  kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}-controller" --timeout=300s
  kubectl -n "$NAMESPACE" rollout status "daemonset/${RELEASE}-node" --timeout=300s
done

# ------------------------------------------------------------------------------
# Verify
# ------------------------------------------------------------------------------
echo ""
echo "==> StorageClasses"
kubectl get storageclass -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,EXPANSION:.allowVolumeExpansion,RECLAIM:.reclaimPolicy,BINDING:.volumeBindingMode'

echo ""
echo "==> VolumeSnapshotClasses"
kubectl get volumesnapshotclass -o custom-columns='NAME:.metadata.name,DRIVER:.driver,DELETION:.deletionPolicy'

echo ""
echo "==> CSI drivers registered on every node"
for driver in iscsi nfs; do
  want="$(kubectl get nodes --no-headers | grep -c .)"
  got="$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/instance="democratic-csi-${driver}" \
          --field-selector=status.phase=Running --no-headers 2>/dev/null | grep -c 'node' || echo 0)"
  printf '    %-24s %s/%s node pods running\n' "democratic-csi-${driver}" "$got" "$want"
done

echo ""
echo "Both classes are installed but UNPROVEN. Before migrating any real data,"
echo "run the verification gate in infra/democratic-csi/README.md — it provisions"
echo "a throwaway PVC on each class, snapshots it, restores it, and expands it."
