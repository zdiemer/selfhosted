#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running gamedex release.
#
# Flow:
#   1. helm upgrade --install
#   2. Wait for rollout
#   3. Print pod status
#
# NOTE: rebuild + side-load the image first with ./build.sh if you changed the
# app code or Dockerfile (imagePullPolicy is IfNotPresent — k3s won't re-pull a
# locally-imported tag).

set -euo pipefail

RELEASE="${RELEASE:-gamedex}"
NAMESPACE="${NAMESPACE:-games}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
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
