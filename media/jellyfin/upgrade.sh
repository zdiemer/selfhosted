#!/usr/bin/env bash
# Apply the current chart to the running jellyfin release.
#
# Flow:
#   1. helm upgrade
#   2. Wait for the rollout
#   3. Print pod status
#
# No values.local.yaml: this chart has no secrets (Jellyfin's user DB lives in
# its config volume, created through the first-run wizard).

set -euo pipefail

RELEASE="${RELEASE:-jellyfin}"
NAMESPACE="${NAMESPACE:-media}"
HERE="$(cd "$(dirname "$0")" && pwd)"

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

GPU_SELECTOR="media.zachd/vaapi=true"
GPU_NODE_COUNT="$(kubectl get nodes -l "$GPU_SELECTOR" -o name | wc -l)"
if [[ "$GPU_NODE_COUNT" -lt 1 ]]; then
  echo "No nodes carry $GPU_SELECTOR; label a verified VAAPI node before deploying." >&2
  exit 1
fi
if [[ "$GPU_NODE_COUNT" -lt 2 ]]; then
  echo "[WARNING] Only one node carries $GPU_SELECTOR; transcoding has no host failover." >&2
fi
echo "==> VAAPI nodes"
kubectl get nodes -l "$GPU_SELECTOR"

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "${HERE}/values.yaml" --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
