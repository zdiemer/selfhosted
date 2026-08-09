#!/usr/bin/env bash
# Apply the current chart to the running jellyseerr release.
#
# Flow:
#   1. helm upgrade
#   2. Wait for the rollout
#   3. Print pod status
#
# No values.local.yaml: this chart has no secrets. Jellyseerr's connections to
# Jellyfin/Radarr/Sonarr (and their API keys) are configured in its web UI and
# live in the config PVC.

set -euo pipefail

RELEASE="${RELEASE:-jellyseerr}"
NAMESPACE="${NAMESPACE:-media}"
HERE="$(cd "$(dirname "$0")" && pwd)"

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "${HERE}/values.yaml"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
