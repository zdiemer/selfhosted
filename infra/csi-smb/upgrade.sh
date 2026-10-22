#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# renovate: datasource=helm depName=csi-driver-smb registryUrl=https://raw.githubusercontent.com/kubernetes-csi/csi-driver-smb/master/charts
CHART_VERSION=1.19.0
helm repo add csi-driver-smb https://raw.githubusercontent.com/kubernetes-csi/csi-driver-smb/master/charts --force-update >/dev/null
helm repo update csi-driver-smb >/dev/null
helm upgrade --install csi-driver-smb csi-driver-smb/csi-driver-smb \
  --version "$CHART_VERSION" -n kube-system -f "$HERE/values.yaml" \
  --post-renderer "$HERE/post-render.py"
kubectl -n kube-system rollout status deployment/csi-smb-controller --timeout=300s
kubectl -n kube-system rollout status daemonset/csi-smb-node --timeout=600s
