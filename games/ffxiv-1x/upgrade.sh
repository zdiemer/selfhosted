#!/usr/bin/env bash
# Apply the chart to the ffxiv-1x release (namespace: games). No secrets: the
# server has no admin account; accounts are created through the launcher's
# signup form on the web port.

set -euo pipefail

RELEASE="${RELEASE:-ffxiv-1x}"
NAMESPACE="${NAMESPACE:-games}"
HERE="$(cd "$(dirname "$0")" && pwd)"

K="kubectl -n ${NAMESPACE}"
command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" --create-namespace \
  -f "${HERE}/values.yaml" --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
