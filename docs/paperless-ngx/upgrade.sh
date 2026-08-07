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

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s
$K rollout status "deployment/${RELEASE}-postgres" --timeout=120s
$K rollout status "deployment/${RELEASE}-redis" --timeout=60s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
