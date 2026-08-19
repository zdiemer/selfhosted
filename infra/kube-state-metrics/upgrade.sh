#!/usr/bin/env bash
# Apply infra/kube-state-metrics — the upstream chart with our values.
# Kubernetes object state as Prometheus series; scraped by infra/alloy.
#
# Nothing depends on this for serving traffic. A bad deploy loses metrics, not
# workloads. The one thing worth checking after every deploy is the series
# count: unfiltered KSM is ~15-20k series against a 10k free tier, so the
# allowlist in values.yaml is the only thing between this and a throttled
# stack. upgrade.sh asserts it rather than trusting it.

set -euo pipefail

RELEASE="${RELEASE:-kube-state-metrics}"
NAMESPACE="${NAMESPACE:-infra}"
# Pin the chart. KSM chart bumps have historically changed the DEFAULT
# collector set, which is exactly the kind of change that costs money quietly.
# renovate: datasource=helm depName=kube-state-metrics registryUrl=https://prometheus-community.github.io/helm-charts
CHART_VERSION="${CHART_VERSION:-8.3.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

# Above this, something has gone wrong with the allowlist. ~1.5k is the design
# target; 3000 is generous headroom that still fires long before the 10k cap.
BUDGET="${BUDGET:-3000}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} prometheus-community/kube-state-metrics@${CHART_VERSION} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" prometheus-community/kube-state-metrics --cleanup-on-fail \
  --version "$CHART_VERSION" -n "$NAMESPACE" -f "$VALUES"

echo "==> waiting for the Deployment"
kubectl -n "$NAMESPACE" rollout status deployment/"$RELEASE" --timeout=180s

# THE CHECK THAT MATTERS. A chart bump that re-enables default collectors, or
# an allowlist typo (KSM silently ignores names it doesn't recognise), both
# show up here and nowhere else until the stack starts refusing writes.
echo "==> series count (design target ~1.5k, fails above ${BUDGET})"
kubectl -n "$NAMESPACE" port-forward "deploy/${RELEASE}" 18080:8080 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 3

METRICS="$(curl -sf --max-time 20 localhost:18080/metrics || true)"
[[ -n "$METRICS" ]] || { echo "ERROR: could not scrape ${RELEASE}"; exit 1; }

COUNT="$(printf '%s\n' "$METRICS" | grep -cve '^#' || true)"
echo "    ${COUNT} series"
printf '%s\n' "$METRICS" | grep -v '^#' | sed 's/[ {].*//' | sort | uniq -c | sort -rn | head -10

if [[ "$COUNT" -gt "$BUDGET" ]]; then
  echo
  echo "ERROR: ${COUNT} series exceeds the ${BUDGET} budget."
  echo "       The free tier caps ACTIVE SERIES at 10k across the whole stack."
  echo "       Check collectors/metricAllowlist in values.yaml against the"
  echo "       family counts above before letting Alloy scrape this."
  exit 1
fi

# An allowlist entry KSM doesn't recognise is silently ignored, so a typo looks
# exactly like a metric that happens to have no instances right now. Report the
# ones that produced nothing; some are legitimately empty (no failed jobs is
# good news) but a misspelling will sit in this list forever.
echo "==> allowlist entries producing no series (typo, or genuinely nothing to report)"
SCRAPE="$(mktemp)"
trap 'kill $PF 2>/dev/null || true; rm -f "$SCRAPE"' EXIT
printf '%s\n' "$METRICS" >"$SCRAPE"
# Both inputs arrive as argv: the script itself is the heredoc on stdin, so the
# scrape cannot also be piped in.
python3 - "$VALUES" "$SCRAPE" <<'PY' || true
import sys, yaml
names = set()
with open(sys.argv[2]) as fh:
    for line in fh:
        if line and not line.startswith('#'):
            names.add(line.split('{')[0].split(' ')[0])
allow = yaml.safe_load(open(sys.argv[1])).get('metricAllowlist') or []
missing = [m for m in allow if m not in names]
print("    " + (", ".join(missing) if missing else "(none — every entry is producing data)"))
PY
