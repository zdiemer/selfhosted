#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running kelsey-green release.
#
# NOTE: there is no build step and no image to push. The site itself ships via
# GitHub Actions → the `deploy` branch → the git-sync sidecar. Only run this
# when the CHART changes; content changes need nothing from you.

set -euo pipefail

RELEASE="${RELEASE:-kelsey-green}"
NAMESPACE="${NAMESPACE:-web}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES} — copy values.local.yaml.example and add the deploy key"
  exit 1
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
