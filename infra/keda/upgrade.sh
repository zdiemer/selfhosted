#!/usr/bin/env bash
set -euo pipefail
# KEDA 2.21 covers Kubernetes 1.34–1.36. Upgrade separately before K3s.
# renovate: datasource=helm depName=keda registryUrl=https://kedacore.github.io/charts
CHART_VERSION="${CHART_VERSION:-2.21.1}"
helm upgrade --install keda keda \
  --repo https://kedacore.github.io/charts \
  --version "$CHART_VERSION" --namespace keda --create-namespace \
  --atomic --wait --timeout 5m
