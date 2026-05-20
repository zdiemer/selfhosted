#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running KeePass release.
#
# checksum/secret on the WebDAV Deployment picks up Secret diffs and cycles
# the pod automatically; KeeWeb is a static SPA and changes only when the
# image is bumped.

set -euo pipefail

RELEASE="${RELEASE:-keepass}"
NAMESPACE="${NAMESPACE:-auth}"
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

echo "==> Waiting for rollouts"
$K rollout status "deployment/${RELEASE}-webdav" --timeout=300s
$K rollout status "deployment/${RELEASE}-keeweb" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
