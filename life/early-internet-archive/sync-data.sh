#!/usr/bin/env bash
# Copy the complete local preservation dataset into the NAS-backed archive PVC.
# The web Deployment mounts this claim read-only; this short-lived pod is the
# only chart-adjacent component that ever mounts it read-write.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA_ROOT="${DATA_ROOT:-${HERE}/data}"
NAMESPACE="${NAMESPACE:-life}"
RELEASE="${RELEASE:-early-internet-archive}"
CLAIM="${CLAIM:-${RELEASE}-archive}"
POD="${POD:-${RELEASE}-data-sync}"
BUSYBOX_IMAGE="${BUSYBOX_IMAGE:-busybox:1.37.0}"

command -v helm >/dev/null || { echo "helm required" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required" >&2; exit 1; }
command -v tar >/dev/null || { echo "tar required" >&2; exit 1; }
[[ -d "$DATA_ROOT" ]] || { echo "archive data directory not found: $DATA_ROOT" >&2; exit 1; }
[[ -s "$DATA_ROOT/library.sqlite3" ]] || {
  echo "catalog missing: run ./build_library.py before syncing" >&2
  exit 1
}

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

if ! kubectl -n "$NAMESPACE" get pvc "$CLAIM" >/dev/null 2>&1; then
  if [[ "$CLAIM" != "${RELEASE}-archive" ]]; then
    echo "PVC $NAMESPACE/$CLAIM does not exist" >&2
    exit 1
  fi
  echo "==> Creating NAS-backed PVC $NAMESPACE/$CLAIM"
  helm template "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$HERE/values.yaml" \
    --show-only templates/pvc.yaml | kubectl -n "$NAMESPACE" apply -f -
fi

# A claim created before the first Helm install needs the same ownership
# metadata Helm would record itself, otherwise the later install correctly
# refuses to adopt an object that could belong to another release.
kubectl -n "$NAMESPACE" label pvc "$CLAIM" \
  app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
kubectl -n "$NAMESPACE" annotate pvc "$CLAIM" \
  "meta.helm.sh/release-name=${RELEASE}" \
  "meta.helm.sh/release-namespace=${NAMESPACE}" --overwrite >/dev/null

cleanup() {
  kubectl -n "$NAMESPACE" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "==> Starting scoped PVC writer pod"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${POD}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: early-internet-archive-data-sync
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    fsGroup: 65532
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: sync
      image: ${BUSYBOX_IMAGE}
      command: ["/bin/sh", "-c", "sleep 86400"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: archive
          mountPath: /archive
  volumes:
    - name: archive
      persistentVolumeClaim:
        claimName: ${CLAIM}
EOF

kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$POD" --timeout=180s

LOCAL_BYTES="$(du -sb "$DATA_ROOT" | awk '{print $1}')"
echo "==> Copying $(du -sh "$DATA_ROOT" | awk '{print $1}') into PVC $CLAIM"
tar -C "$DATA_ROOT" -cf - . | kubectl -n "$NAMESPACE" exec -i "$POD" -- tar -C /archive -xof -

LOCAL_CATALOG_SHA="$(sha256sum "$DATA_ROOT/library.sqlite3" | awk '{print $1}')"
REMOTE_CATALOG_SHA="$(kubectl -n "$NAMESPACE" exec "$POD" -- sha256sum /archive/library.sqlite3 | awk '{print $1}')"
[[ "$LOCAL_CATALOG_SHA" == "$REMOTE_CATALOG_SHA" ]] || {
  echo "catalog checksum mismatch after PVC sync" >&2
  exit 1
}

REMOTE_BYTES="$(kubectl -n "$NAMESPACE" exec "$POD" -- du -sb /archive | awk '{print $1}')"
echo "==> PVC sync complete"
echo "    local bytes:  $LOCAL_BYTES"
echo "    remote bytes: $REMOTE_BYTES"
echo "    catalog sha:  $REMOTE_CATALOG_SHA"
