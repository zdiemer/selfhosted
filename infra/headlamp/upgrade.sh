#!/usr/bin/env bash
# Apply the current values.yaml to the Headlamp release.
#
# Stock upstream chart, so this only adds the repo, ensures the namespace, and
# upgrades. `--install` means the same script bootstraps a fresh cluster and
# upgrades an existing one; talaria's version had separate install/upgrade
# subcommands for what is one idempotent operation.
#
# Run ./token.sh afterwards for a login token.

set -euo pipefail

RELEASE="${RELEASE:-headlamp}"
NAMESPACE="${NAMESPACE:-headlamp}"
CHART="${CHART:-headlamp/headlamp}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> Ensuring the Headlamp helm repo"
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/ >/dev/null 2>&1 || true
helm repo update headlamp >/dev/null

# One namespace per project; created here rather than by the chart.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${CHART} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES"

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

NODE_PORT="$($K get svc "$RELEASE" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"
NODE_IP="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalAddress")].address}' 2>/dev/null)"
[[ -z "$NODE_IP" ]] && NODE_IP="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"

# Say out loud what this thing can do, every time it's deployed.
cat <<EOF

==> Headlamp is up
    URL:   http://${NODE_IP}:${NODE_PORT:-30100}   (LAN only — any node IP works)
    Token: ./token.sh

    The ServiceAccount behind that token is bound to cluster-admin. The token IS
    a full cluster credential. It is not behind Authelia and not behind the
    tunnel; the NodePort is open on every node on the LAN.
EOF
