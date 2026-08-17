#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running bitmagnet release.
#
# Flow:
#   1. Resolve the Postgres password from 1Password into memory
#   2. helm upgrade --install
#   3. Wait for both rollouts (postgres first — bitmagnet crash-loops until the
#      database answers)
#   4. Print pods + the crawl counters, which is the only real proof it works

set -euo pipefail

RELEASE="${RELEASE:-bitmagnet}"
NAMESPACE="${NAMESPACE:-media}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Secrets are resolved from 1Password into memory for the life of this run and
# never written to disk. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

# An empty resolve would render a Postgres with a blank password, which the
# postgres image refuses to start with — fail here instead, where the reason is
# legible.
sv_has || { echo "FAIL: no bitmagnet secrets resolved from 1Password."
            echo "  check with:  ./scripts/secrets.sh check media/bitmagnet"; exit 1; }

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for ${RELEASE}-postgres rollout"
$K rollout status "deployment/${RELEASE}-postgres" --timeout=300s
echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"

echo "==> Torznab endpoint"
$K exec "deploy/${RELEASE}" -- wget -qO- "http://localhost:3333/torznab/api?t=caps" | head -5 || true
echo
