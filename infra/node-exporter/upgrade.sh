#!/usr/bin/env bash
# Apply infra/node-exporter — the upstream prometheus-node-exporter chart with
# our values. Host-level metrics for every node; scraped by infra/alloy.
#
# Nothing depends on this for serving traffic. A bad deploy here loses node
# metrics, not ingress. It does bind :9100 on the host network of every node,
# so the one genuine failure mode is a port collision — checked for below,
# because "address already in use" on one node out of ten is easy to miss.

set -euo pipefail

RELEASE="${RELEASE:-node-exporter}"
NAMESPACE="${NAMESPACE:-infra}"
# Pin the chart. A surprise bump is how a quiet exporter turns into a pod that
# won't start, or worse, one that starts with different default collectors and
# silently doubles the series bill.
# renovate: datasource=helm depName=prometheus-node-exporter registryUrl=https://prometheus-community.github.io/helm-charts
CHART_VERSION="${CHART_VERSION:-4.56.1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} prometheus-community/prometheus-node-exporter@${CHART_VERSION} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" prometheus-community/prometheus-node-exporter \
  --version "$CHART_VERSION" -n "$NAMESPACE" -f "$VALUES"

echo "==> waiting for the DaemonSet"
kubectl -n "$NAMESPACE" rollout status daemonset/"${RELEASE}-prometheus-node-exporter" --timeout=180s

# A DaemonSet reports Ready per node, so a single node failing to bind :9100
# shows up here and nowhere else. Compare desired against ready explicitly
# rather than trusting the rollout, which has already returned success.
echo "==> node coverage"
read -r DESIRED READY < <(kubectl -n "$NAMESPACE" get daemonset "${RELEASE}-prometheus-node-exporter" \
  -o jsonpath='{.status.desiredNumberScheduled} {.status.numberReady}')
NODES="$(kubectl get nodes --no-headers | wc -l)"
echo "    nodes=${NODES} desired=${DESIRED} ready=${READY}"
if [[ "$READY" != "$DESIRED" ]]; then
  echo "WARN: ${READY}/${DESIRED} ready — check for a :9100 port collision:"
  echo "      kubectl -n ${NAMESPACE} get pods -l app.kubernetes.io/name=prometheus-node-exporter -o wide"
  echo "      kubectl -n ${NAMESPACE} logs -l app.kubernetes.io/name=prometheus-node-exporter --tail=20"
fi

# Prove the exported series actually match the budget this chart is built
# around. The collector keep-list is the whole design; if a chart upgrade
# quietly re-enables defaults, THIS is the number that moves, and it moves
# before the Grafana Cloud bill does.
echo "==> series count per node (measured baseline ~570 raw; Alloy keeps ~180)"
POD="$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=prometheus-node-exporter \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "$NAMESPACE" port-forward "pod/${POD}" 19100:9100 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 3
COUNT="$(curl -sf --max-time 10 localhost:19100/metrics | grep -cve '^#' || true)"
echo "    ${POD}: ${COUNT} series"
if [[ "$COUNT" -gt 800 ]]; then
  echo "WARN: well above budget. Top metric families:"
  curl -sf --max-time 10 localhost:19100/metrics | grep -v '^#' | sed 's/[ {].*//' \
    | sort | uniq -c | sort -rn | head -10
fi
