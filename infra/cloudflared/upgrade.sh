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
# The secrets are resolved from 1Password into memory for the life of this run
# and never written to a disk. Each helm call spells out its own `-f <(sv_fd)`
# rather than carrying it in VALUE_ARGS: that fd is a pipe, readable once, so a
# shared one would hand the second reader an empty values file. See
# scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "no secrets resolved from 1Password — copy values.local.yaml.example and add the tunnel token"
  echo "  check with:  ./scripts/secrets.sh check infra/cloudflared"
  exit 1
fi

# One namespace per project; created manually, never chart-managed.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --atomic --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=120s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
