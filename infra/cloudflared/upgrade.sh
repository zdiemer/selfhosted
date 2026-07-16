#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running cloudflared release.
#
# This runs the shared Cloudflare Tunnel connector for the diemer.codes zone. The
# public-hostname -> service routing lives on the tunnel in the Cloudflare
# dashboard, not here; this only runs the connector and feeds it the token.

set -euo pipefail

RELEASE="${RELEASE:-cloudflared}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES} — copy values.local.yaml.example and add the tunnel token"
  exit 1
fi

# One namespace per project; created manually, never chart-managed.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=120s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
