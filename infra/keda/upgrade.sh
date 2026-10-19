#!/usr/bin/env bash
set -euo pipefail
# KEDA 2.20 covers Kubernetes 1.33–1.35. Upgrade separately before K3s.
helm upgrade --install keda keda \
  --repo https://kedacore.github.io/charts \
  --version 2.20.0 --namespace keda --create-namespace \
  --atomic --wait --timeout 5m
