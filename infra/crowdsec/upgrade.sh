#!/usr/bin/env bash
# Apply infra/crowdsec — the upstream crowdsec/crowdsec chart with our values.
# Detection-only: agents parse the Traefik access log, the LAPI records
# decisions, nothing enforces them. See values.yaml for the full rationale.
#
# Console enrollment is optional; if a values.local.yaml (or its 1Password
# template) is present it is layered on. Nothing here is load-bearing for
# ingress — a bad deploy breaks detection, not traffic.

set -euo pipefail

RELEASE="${RELEASE:-crowdsec}"
NAMESPACE="${NAMESPACE:-crowdsec}"
# Pin the chart: a surprise chart bump is how a quiet log-shipper turns into a
# pod that won't start. Bump deliberately, reading the chart changelog.
# renovate: datasource=helm depName=crowdsec registryUrl=https://crowdsecurity.github.io/helm-charts
CHART_VERSION="${CHART_VERSION:-0.24.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "WARN: op inject failed — continuing without console enrollment"; rm -f "$LOCAL_VALUES"; }
  [[ -f "$LOCAL_VALUES" ]] && chmod 600 "$LOCAL_VALUES"
fi

VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

helm repo add crowdsec https://crowdsecurity.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update crowdsec >/dev/null

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} crowdsec/crowdsec@${CHART_VERSION} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" crowdsec/crowdsec --cleanup-on-fail \
  --version "$CHART_VERSION" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> waiting for the LAPI"
kubectl -n "$NAMESPACE" rollout status deployment/crowdsec-lapi --timeout=180s

echo "==> acquisition check (want non-zero traefik lines once traffic flows)"
kubectl -n "$NAMESPACE" exec deploy/crowdsec-lapi -- cscli metrics 2>/dev/null | head -30 || \
  echo "    (metrics come from the agent side; give it a minute and run:"
echo "     kubectl -n ${NAMESPACE} exec ds/crowdsec-agent -- cscli metrics)"
