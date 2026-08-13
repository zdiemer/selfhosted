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
# The secrets are resolved from 1Password into memory for the life of this run
# and never written to a disk. Each helm call spells out its own `-f <(sv_fd)`
# rather than carrying it in VALUE_ARGS: that fd is a pipe, readable once, so a
# shared one would hand the second reader an empty values file. See
# scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ "${1:-}" == "--build" ]]; then
  "${HERE}/build.sh"
fi

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Tailing bridge logs (Ctrl-C to exit; bridge keeps running)"
exec $K logs -f "deployment/${RELEASE}"
