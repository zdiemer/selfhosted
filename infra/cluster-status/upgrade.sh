#!/usr/bin/env bash
# Apply the cluster-status chart.
#
# This page is public, so the deploy check is deliberately about what's being
# published, not just whether pods came up: it prints what the collector is
# actually writing into status.json before you walk away.

set -euo pipefail

RELEASE="${RELEASE:-cluster-status}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

# The collector needs one full interval before status.json exists at all, and two
# before network rates appear (they're a diff against the previous sample).
echo "==> Waiting for the first scrape"
POD="$($K get pod -l app.kubernetes.io/instance="${RELEASE}" -o jsonpath='{.items[0].metadata.name}')"
for _ in $(seq 1 30); do
  if $K exec "$POD" -c collector -- test -f /data/status.json 2>/dev/null; then break; fi
  sleep 2
done

if ! $K exec "$POD" -c collector -- test -f /data/status.json 2>/dev/null; then
  echo "FAIL: collector never wrote /data/status.json"
  $K logs "$POD" -c collector --tail=20
  exit 1
fi

# Read the payload the collector produced and say plainly what is now public.
# Deliberately prints the field list: if a `publish.*` flag was meant to be off,
# this is where you'd notice it isn't.
echo "==> What the page is publishing"
$K exec "$POD" -c collector -- python3 -c '
import json
d = json.load(open("/data/status.json"))
t = d.get("totals") or {}
n = (d.get("nodeDisks") or [{}])[0]
print("    collected:   %s" % d.get("generatedAt"))
print("    nodes:       %s/%s ready" % (t.get("readyNodeCount"), t.get("nodeCount")))
print("    pods:        %s" % t.get("podCount"))
print("    deployments: %d" % len(d.get("deployments") or []))
print("    events:      %d warning(s)" % len(d.get("recentEvents") or []))
print("    node fields: %s" % ", ".join(sorted(n.keys())))
print("")
print("    PUBLIC - anyone can read all of the above at the URLs below.")
'

echo "==> Ingress"
$K get ingress "$RELEASE"
