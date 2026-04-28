#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running claude-bridge.
#
# No pre-flight needed — the bridge holds no durable state worth flushing
# (sessions and the cloned repo live on a PVC and survive pod restarts).
# The Minecraft pod is NOT touched by this script.
#
# Flow:
#   1. (optional) build.sh — only if you've edited image source or settings
#   2. helm upgrade
#   3. wait for rollout, tail logs

set -euo pipefail

RELEASE="${RELEASE:-claude-bridge}"
NAMESPACE="${NAMESPACE:-minecraft}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ "${1:-}" == "--build" ]]; then
  "${HERE}/build.sh"
fi

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Tailing bridge logs (Ctrl-C to exit; bridge keeps running)"
exec $K logs -f "deployment/${RELEASE}"
