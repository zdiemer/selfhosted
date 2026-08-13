#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running paperless-ngx
# release.
#
# Flow:
#   1. helm upgrade
#   2. Wait for the paperless rollout (the slowest pod — Tika/Gotenberg/Redis
#      come up faster and finish before this returns)
#   3. Print pod status

set -euo pipefail

RELEASE="${RELEASE:-paperless}"
NAMESPACE="${NAMESPACE:-docs}"
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

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s
$K rollout status "deployment/${RELEASE}-postgres" --timeout=120s
$K rollout status "deployment/${RELEASE}-redis" --timeout=60s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
