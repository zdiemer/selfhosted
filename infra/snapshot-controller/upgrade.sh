#!/usr/bin/env bash
# Apply the CSI snapshot controller.
#
# This must be installed BEFORE infra/democratic-csi: a VolumeSnapshotClass is an
# instance of a CRD this chart owns, so without it democratic-csi's snapshot
# classes are unrecognized types that helm accepts and nothing ever acts on.
#
# The one genuinely dangerous thing here is `helm uninstall` — see below.

set -euo pipefail

RELEASE="${RELEASE:-snapshot-controller}"
NAMESPACE="${NAMESPACE:-kube-system}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUE_ARGS=(-f "${HERE}/values.yaml")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

CRDS=(
  volumesnapshots.snapshot.storage.k8s.io
  volumesnapshotcontents.snapshot.storage.k8s.io
  volumesnapshotclasses.snapshot.storage.k8s.io
)

# Report what would be destroyed if these CRDs went away, so the number is in
# front of you before and after rather than in a postmortem.
count_snapshots() {
  # `grep -c .` prints 0 AND exits 1 on empty input, so a `|| echo 0` fallback
  # would emit a second 0 and the caller would compare "0\n0". Swallow the exit
  # status instead and let grep's own count stand.
  kubectl get volumesnapshots -A --no-headers 2>/dev/null | grep -c . || true
}

BEFORE="$(count_snapshots)"
echo "==> VolumeSnapshots currently in the cluster: ${BEFORE}"

# Chart.lock and charts/*.tgz are gitignored local state; refresh them from
# Chart.yaml so a merged chart-bump PR actually deploys the new version.
echo "==> helm dependency update"
helm dependency update "$HERE" >/dev/null

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --cleanup-on-fail

echo "==> Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}" --timeout=180s

# ------------------------------------------------------------------------------
# Protect the CRDs from `helm uninstall`
# ------------------------------------------------------------------------------
# The upstream chart renders these into templates/ with no resource-policy, so
# helm considers them release-owned and deletes them on uninstall. Deleting a CRD
# cascade-deletes every object of that type — every VolumeSnapshot and
# VolumeSnapshotContent in the cluster — and a VolumeSnapshotContent with
# deletionPolicy: Delete tells the CSI driver to remove the underlying ZFS
# snapshot on the NAS as it goes.
#
# So an `helm uninstall snapshot-controller`, which reads like a reversible
# "remove the controller", would silently destroy every snapshot on the cluster
# AND on the NAS. This annotation makes helm leave the CRDs behind instead.
echo "==> Pinning CRDs against helm uninstall"
for crd in "${CRDS[@]}"; do
  kubectl get crd "$crd" >/dev/null 2>&1 || { echo "    MISSING: $crd"; continue; }
  kubectl annotate --overwrite crd "$crd" helm.sh/resource-policy=keep >/dev/null
  printf '    %-50s keep\n' "$crd"
done

# ------------------------------------------------------------------------------
# Verify the types are actually usable
# ------------------------------------------------------------------------------
# Stock k3s answers `the server doesn't have a resource type "volumesnapshotclass"`.
# If that is still the answer, democratic-csi will install cleanly and snapshot
# nothing.
echo "==> Verify"
if ! kubectl get volumesnapshotclass >/dev/null 2>&1; then
  echo "FAIL: volumesnapshotclass is still not a known resource type."
  echo "      democratic-csi's snapshot classes would be silently inert."
  exit 1
fi
echo "    [OK] volumesnapshotclass / volumesnapshot / volumesnapshotcontent registered"

AFTER="$(count_snapshots)"
echo "    VolumeSnapshots: ${BEFORE} -> ${AFTER}"
if [[ "$AFTER" -lt "$BEFORE" ]]; then
  echo "    WARNING: snapshot count DROPPED. Investigate before taking any more."
  exit 1
fi

kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/instance="${RELEASE}"
