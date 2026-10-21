#!/usr/bin/env bash
# Provision TLS and named node profiles, deploy, then check the local worker.

set -euo pipefail

RELEASE="${RELEASE:-buildkit}"
NAMESPACE="${NAMESPACE:-buildkit}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUE_ARGS=(-f "${HERE}/values.yaml")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

python3 "$HERE/../../scripts/internal-pki.py"
python3 "$HERE/apply-node-profile.py"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --cleanup-on-fail

echo "==> Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}" --timeout=300s

# The readiness probe already runs this, but run it once more visibly: one
# `oci` worker listed = builds will work.
echo "==> Smoke: buildctl debug workers"
kubectl -n "$NAMESPACE" exec "deploy/${RELEASE}" -- \
  buildctl --addr unix:///run/user/1000/buildkit/buildkitd.sock debug workers
