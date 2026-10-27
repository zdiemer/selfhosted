#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE=${RELEASE:-terraria}
NAMESPACE=${NAMESPACE:-games}
if [[ ${1:-} == --build ]]; then "$HERE/build.sh"; fi
helm lint "$HERE"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" --create-namespace \
  -f "$HERE/values.yaml" --wait --timeout 35m --cleanup-on-fail
kubectl -n "$NAMESPACE" get pods -l "app.kubernetes.io/instance=$RELEASE"
