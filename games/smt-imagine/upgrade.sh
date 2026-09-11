#!/usr/bin/env bash
# Apply the chart + secrets to the smt-imagine release (namespace: games).
#
# Nothing here waits for players: a solo server has none to warn. The world
# does flush character state on SIGTERM, and terminationGracePeriodSeconds
# covers it — but a character mid-zone-transition at the exact moment of the
# rollout can still lose the last few seconds. Don't deploy while logged in.

set -euo pipefail

RELEASE="${RELEASE:-smt-imagine}"
NAMESPACE="${NAMESPACE:-games}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Secrets are resolved from 1Password into memory and handed to helm as a
# fresh pipe per call — never written to disk. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" --create-namespace \
  -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"

# The channel refuses to serve a zone whose BinaryData it cannot find, and it
# says so only in its log. An empty data PVC looks exactly like a healthy pod
# from the outside, so check the one thing that distinguishes them.
POD="$($K get pod -l app.kubernetes.io/instance="${RELEASE}" \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -n "$POD" ]] && ! $K exec "$POD" -c channel -- test -d /var/lib/comp_hack/data/BinaryData/Shield 2>/dev/null; then
  echo "WARN: no client data on the data PVC — run ./stage-client.sh <client dir> before anyone logs in." >&2
fi
