#!/usr/bin/env bash
# Apply the current chart to the running old-diemer-codes release.
#
# NOTE: if you moved the site/ submodule pin, or changed the Dockerfile,
# nginx.conf or the overlay, bump the tag (Chart.yaml version + appVersion and
# values.yaml image.tag move together) and run ./build.sh first. imagePullPolicy
# is IfNotPresent, so reusing a tag will NOT re-pull and nothing will change.

set -euo pipefail

RELEASE="${RELEASE:-old-diemer-codes}"
NAMESPACE="${NAMESPACE:-web}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
# There are no secrets in this chart — the site is static, with no env, no API
# and no credentials — so values.local.yaml normally does not exist. Honoured
# anyway, for consistency with every other project here.
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
