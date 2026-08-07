#!/usr/bin/env bash
# Cluster egress inventory: what does this cluster actually talk to, and which
# workload is doing the talking?
#
#   ./scripts/egress-audit.sh status    # is the window open, and how much is captured
#   ./scripts/egress-audit.sh report    # fetch the aggregate and print the inventory
#   ./scripts/egress-audit.sh fetch     # just save the raw JSON
#
# The collection itself runs in the cluster — see infra/coredns-config. Every
# outbound connection starts with a DNS lookup, so the CoreDNS query log sees
# all of it, including from workloads deployed out of repos this one cannot read
# (whatnowgg, talaria).
#
# WHY THIS NEVER TOUCHES LOKI. Shipping the DNS log to Grafana Cloud was the
# obvious design and is the wrong one: several GB/month against a 50 GB/month
# free tier, to answer a question that gets asked once and then acted on. The
# collector aggregates in-cluster and this fetches a few KB. The permanent
# egress signal is the proxy access log in infra/egress-proxy, which is bounded
# by the number of opted-in services and attributed by authenticated name.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-infra}"
DIR="${REPO}/.egress-audit"
MODE="${1:-report}"
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done

JSON="${DIR}/inventory.json"
SELECTOR="app.kubernetes.io/name=coredns-config,app.kubernetes.io/component=collector"

command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }

collector_pod() {
  kubectl -n "$NAMESPACE" get pod -l "$SELECTOR" \
    --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

window_closed_help() {
  cat <<'EOF'
The measurement window is not open.

    1. Open it (this restarts CoreDNS — one replica, so a brief DNS gap):
         echo 'dnsLogging: {enabled: true}' > infra/coredns-config/values.local.yaml
         ./infra/coredns-config/upgrade.sh

    2. Let it run. A day or two is usually enough; the collector survives its
       own restarts and CoreDNS's.

    3. Read it:
         ./scripts/egress-audit.sh report

    4. Close it again — this is an audit instrument, not a pipeline:
         rm infra/coredns-config/values.local.yaml
         ./infra/coredns-config/upgrade.sh
EOF
}

# ---------------------------------------------------------------------------
fetch() {
  POD="$(collector_pod)"
  if [[ -z "$POD" ]]; then
    echo "No running collector in namespace '${NAMESPACE}'."
    echo
    window_closed_help
    exit 1
  fi
  mkdir -p "$DIR"
  # `cat` rather than `kubectl cp`: cp shells out to tar inside the container,
  # which the python:alpine image does not carry.
  if ! kubectl -n "$NAMESPACE" exec "$POD" -- cat /data/inventory.json > "${JSON}.partial" 2>/dev/null; then
    rm -f "${JSON}.partial"
    echo "Collector ${POD} has not written an aggregate yet."
    echo "It flushes every 30s by default — give it a moment, then retry."
    exit 1
  fi
  mv "${JSON}.partial" "$JSON"
  echo "==> ${JSON} ($(wc -c < "$JSON") bytes, from ${POD})"
}

# ---------------------------------------------------------------------------
status() {
  POD="$(collector_pod)"
  if [[ -z "$POD" ]]; then
    echo "window: CLOSED"
    echo
    window_closed_help
    exit 0
  fi
  echo "window:    OPEN"
  echo "collector: ${NAMESPACE}/${POD}"
  kubectl -n kube-system get cm coredns-custom -o jsonpath='{.data.egress-audit\.override}' 2>/dev/null \
    | grep -E '^\s*(log|class)' | sed 's/^/           /'
  fetch >/dev/null 2>&1 || true
  # Plain %-formatting, not f-strings: the whole program is a single-quoted
  # shell argument, and an f-string that needs a double quote inside its own
  # braces cannot be escaped through that without breaking one or the other.
  [[ -f "$JSON" ]] && python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print("elapsed:   %.1fh" % ((d["updated"] - d["started"]) / 3600.0))
print("queries:   %d parsed, %d external" % (d["parsed"], d["external"]))
print("workloads: %d attributed, %d unattributed hosts" % (len(d["workloads"]), len(d["unattributed"])))
print("restarts:  %d log-stream reconnects" % d["reconnects"])
' "$JSON"
}

# ---------------------------------------------------------------------------
report() {
  fetch
  JSON="$JSON" REPO="$REPO" python3 - <<'PY'
import glob, json, os, re

d    = json.load(open(os.environ["JSON"]))
repo = os.environ["REPO"]

# Declared egress, per the egress.allowedHosts values contract. Until charts
# start declaring it, everything reads as undeclared — which is the point of
# running this before writing the allowlists, not after.
declared = {}
for values in glob.glob(os.path.join(repo, "**", "values.yaml"), recursive=True):
    try:
        text = open(values, errors="replace").read()
    except OSError:
        continue
    m = re.search(r'^\s*allowedHosts:\s*\n((?:\s*-\s*\S+\n)+)', text, re.M)
    if m:
        chart = os.path.basename(os.path.dirname(values))
        declared[chart] = {h.strip().lstrip("- ").strip('"\'')
                           for h in m.group(1).strip().split("\n")}

def matches(host, patterns):
    return any(host == p or host.endswith("." + p.lstrip(".")) for p in patterns)

hrs = (d["updated"] - d["started"]) / 3600.0
print(f"\n{'='*74}\nCLUSTER EGRESS INVENTORY   ({hrs:.1f}h window)\n{'='*74}")
print(f"{d['parsed']} queries parsed, {d['external']} external, "
      f"{len(d['workloads'])} workloads attributed\n")

for key in sorted(d["workloads"]):
    hosts = d["workloads"][key]
    chart = key.split("/", 1)[1]
    decl  = declared.get(chart, set())
    print(f"{key}   ({len(hosts)} hosts)")
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        flag = "" if not decl or matches(host, decl) else "   <- UNDECLARED"
        print(f"    {n:7d}  {host}{flag}")
    for p in sorted(decl):
        if not any(matches(h, {p}) for h in hosts):
            print(f"    {'':7}  {p}   <- declared, never seen")
    print()

if d["unattributed"]:
    print(f"{'-'*74}\nUNATTRIBUTED — no pod or node held that IP when the query was seen")
    print("(the pod's ADDED event had not arrived yet, or these counts predate")
    print(" a collector change — the aggregate resumes across restarts, so a")
    print(" long window can mix attribution from before and after a fix)\n")
    for host, n in sorted(d["unattributed"].items(), key=lambda kv: -kv[1])[:40]:
        print(f"    {n:7d}  {host}")
    print()

print(f"{'-'*74}")
print("Next: fill egress.allowedHosts per chart from this, then close the window")
print("      (rm infra/coredns-config/values.local.yaml && ./infra/coredns-config/upgrade.sh)")
PY
}

case "$MODE" in
  fetch)  fetch  ;;
  status) status ;;
  report) report ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \?//'; exit 1 ;;
esac
