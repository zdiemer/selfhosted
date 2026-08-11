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

# Materialize values.local.yaml from 1Password when it's missing and a template
# exists. Convenience only: values.local.yaml is still the contract, so this
# no-ops without `op` — e.g. in the claude-workspace pod, which is fed by
# `scripts/secrets.sh publish` instead. See values.local.tpl.yaml.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ "${1:-}" == "--build" ]]; then
  "${HERE}/build.sh"
fi

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Tailing bridge logs (Ctrl-C to exit; bridge keeps running)"
exec $K logs -f "deployment/${RELEASE}"
