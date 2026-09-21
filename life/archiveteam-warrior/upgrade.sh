#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE="${RELEASE:-archiveteam-warrior}"
NAMESPACE="${NAMESPACE:-life}"
args=(-f "$HERE/values.yaml")
[[ ! -f "$HERE/values.local.yaml" ]] || args+=(-f "$HERE/values.local.yaml")

command -v helm >/dev/null
command -v kubectl >/dev/null
# Pending is a valid outcome when the cluster has no spare capacity. Do not
# use --atomic/--wait: they would roll back a correctly unscheduled worker.
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  --create-namespace "${args[@]}" "$@"
kubectl -n "$NAMESPACE" get pods -l "app.kubernetes.io/instance=$RELEASE"
