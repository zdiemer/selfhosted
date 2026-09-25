#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHART="${ROR2_CHART:-${HOME}/Code/ror2-unofficial-dedicated-server-docker/charts/ror2-server}"

helm upgrade --install ror2 "$CHART" -n games --create-namespace \
  -f "$HERE/values.yaml" --cleanup-on-fail
kubectl -n games rollout status deployment/ror2-ror2 --timeout=600s
kubectl -n games get service ror2-ror2
