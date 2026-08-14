#!/usr/bin/env bash
# Apply the cluster-status chart.
#
# The deploy check is deliberately about what's being published, not just
# whether pods came up: it prints what the collector is actually writing into
# status.json before you walk away.
#
# That check was written when the page was public and is if anything MORE
# useful now that it is tailnet-only, because the way this page becomes a
# problem again is a quiet re-listing. The reachability line below reads the
# live Ingress rather than assuming, so re-adding a Cloudflare host shows up
# here on the very next deploy.
#
# It matters more since the page was expanded for a tailnet audience. It now
# carries node IPs, the whole routing table, the pod image inventory, PVC
# names and the probe targets — so the gap between "tailnet only" and "public"
# is much wider than it was, and the interlock at the bottom of this script
# calls out the worst combination specifically.

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
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --atomic --cleanup-on-fail

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

# The collector needs one full interval before status.json exists at all, and two
# before network rates appear (they're a diff against the previous sample).
echo "==> Waiting for the first scrape"
# NEWEST pod, not items[0]. The old selection raced the rollout: `rollout
# status` returns as soon as the new pods are ready, while the outgoing ones are
# still Running and still terminating — and items[0] is in name order, so it
# regularly picked a pod from the PREVIOUS ReplicaSet. The report below then
# described the previous release's status.json, which is the one thing this
# script exists to get right, and it did it silently. The newest pod is
# necessarily from the new ReplicaSet.
POD="$($K get pod -l app.kubernetes.io/instance="${RELEASE}" \
  --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}')"
for _ in $(seq 1 30); do
  if $K exec "$POD" -c collector -- test -f /data/status.json 2>/dev/null; then break; fi
  sleep 2
done

if ! $K exec "$POD" -c collector -- test -f /data/status.json 2>/dev/null; then
  echo "FAIL: collector never wrote /data/status.json"
  $K logs "$POD" -c collector --tail=20
  exit 1
fi

# Read the payload the collector produced and say plainly what is now readable.
# Deliberately prints the field list: if a `publish.*` flag was meant to be off,
# this is where you'd notice it isn't.
echo "==> What the page is publishing"
$K exec "$POD" -c collector -- python3 -c '
import json
d = json.load(open("/data/status.json"))
t = d.get("totals") or {}
n = (d.get("nodeDisks") or [{}])[0]
print("    collected:    %s" % d.get("generatedAt"))
print("    nodes:        %s/%s ready" % (t.get("readyNodeCount"), t.get("nodeCount")))
print("    pods:         %s (%d problem)" % (t.get("podCount"), len(d.get("problemPods") or [])))
print("    workloads:    %d deploy / %d sts / %d ds / %d cronjob" % (
    len(d.get("deployments") or []), len(d.get("statefulSets") or []),
    len(d.get("daemonSets") or []), len(d.get("cronJobs") or [])))
ev = d.get("recentEvents") or []
print("    events:       %d (%d warning) - Normal events are published too" % (
    len(ev), sum(1 for e in ev if e.get("type") == "Warning")))
print("    routing:      %d ingress route(s), %d reachable from the internet" % (
    len(d.get("ingresses") or []),
    sum(1 for i in (d.get("ingresses") or []) if i.get("exposure") == "public")))
print("    storage:      %d PVC(s)" % len(d.get("storage") or []))
print("    namespaces:   %d" % len((d.get("totals") or {}).get("namespaces") or []))
print("    node fields:  %s" % ", ".join(sorted(n.keys())))
# The one that is worth seeing spelled out rather than inferred from a flag.
urls = [s for s in (d.get("services") or []) if s.get("url")]
if urls:
    print("    probe URLs:   PUBLISHED - %s" % ", ".join(s["url"] for s in urls))
print("")
'

# Say who can actually read it, derived from the live Ingress hosts rather than
# asserted. A host that is not *.duckdns is on the tunnel, i.e. public.
PUBLIC_HOSTS="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[*].host}' \
  | tr ' ' '\n' | grep -v '\.duckdns\.org$' || true)"
if [[ -n "$PUBLIC_HOSTS" ]]; then
  echo ""
  echo "    !! PUBLIC - anyone on the internet can read all of the above at:"
  echo "$PUBLIC_HOSTS" | sed 's/^/       https:\/\//'
  echo "    If that is not deliberate, see ingress.cloudflareHosts in values.yaml."
  # The combination that is worse than either half. The page was expanded on
  # the explicit assumption of a tailnet-only audience: node IPs, the routing
  # table with its public/tailnet column, pod image inventory, PVC names and
  # the probe targets. Re-listing this host publishes all of it at once, and
  # the probe URLs are the sharpest of them because they name internal hosts
  # that appear nowhere else.
  if $K exec "$POD" -c collector -- python3 -c '
import json, sys
d = json.load(open("/data/status.json"))
sys.exit(0 if any(s.get("url") for s in (d.get("services") or [])) else 1)
' 2>/dev/null; then
    echo ""
    echo "    !! and serviceCheck URLs are published - internal ClusterIPs and"
    echo "       LAN addresses are readable at those hosts. Set"
    echo "       publish.serviceCheckUrls: false, or delist."
  fi
else
  echo ""
  echo "    Tailnet only - readable by anyone on the tailnet, not the internet."
fi

echo "==> Ingress"
$K get ingress "$RELEASE"
