#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running Vocard release.
#
# No pre-flight flush needed (unlike Minecraft): Mongo commits continuously,
# Lavalink holds no state worth saving, the bot will reconnect to Discord
# gateway on restart.
#
# Flow:
#   1. helm upgrade (Recreate strategy on bot + lavalink; mongo StatefulSet
#      rolls pod-by-pod, but there's only one pod, so same effect)
#   2. Wait for each component's rollout
#   3. Tail bot logs

set -euo pipefail

RELEASE="${RELEASE:-vocard}"
NAMESPACE="${NAMESPACE:-discord}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

for d in bot lavalink; do
  echo "==> Waiting for ${RELEASE}-${d} rollout"
  $K rollout status "deployment/${RELEASE}-${d}" --timeout=300s
done

echo "==> Waiting for ${RELEASE}-mongo rollout"
$K rollout status "statefulset/${RELEASE}-mongo" --timeout=300s

echo "==> Tailing bot logs (Ctrl-C to exit; bot keeps running)"
exec $K logs -f "deployment/${RELEASE}-bot"
