#!/usr/bin/env bash
# Install/upgrade Grafana Alloy from the upstream chart, with our values.
#
# Values-only project (like infra/headlamp and minecraft/): there is no chart
# here, so this adds the upstream repo first. It also builds the Grafana Cloud
# credentials Secret out of values.local.yaml, because the upstream chart has no
# way to create one and the token must not end up in a ConfigMap.
#
# Nothing here touches ingress. Alloy only reads.

set -euo pipefail

RELEASE="${RELEASE:-alloy}"
NAMESPACE="${NAMESPACE:-alloy}"
CHART="${CHART:-grafana/alloy}"
SECRET="alloy-grafana-cloud"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES}"
  echo "  cp values.local.yaml.example values.local.yaml   # then add the Grafana Cloud token"
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# Pull the five credential fields out of values.local.yaml. They go into a
# Secret rather than into the chart values so they never land in the rendered
# ConfigMap, which is world-readable to anyone with namespace access.
read_cred() {
  python3 -c "
import sys, yaml
d = yaml.safe_load(open('${LOCAL_VALUES}')) or {}
v = str((d.get('grafanaCloud') or {}).get('$1', ''))
# Reject the literal values shipped in values.local.yaml.example. '123456' is
# the one that actually bit: it looks like a real instance ID, so it survives a
# read-through, and Grafana Cloud answers every push with HTTP 530 rather than
# anything that says 'wrong tenant'.
PLACEHOLDERS = {'123456', '000000', 'glc_' + '0' * 60}
if not v or 'XXX' in v or v in PLACEHOLDERS or v.startswith('glc_00000'):
    sys.exit(1)
print(v)
"
}

for f in lokiUrl lokiUser promUrl promUser token; do
  if ! read_cred "$f" >/dev/null; then
    echo "FAIL: grafanaCloud.${f} is unset or still an example placeholder in ${LOCAL_VALUES}"
    echo
    echo "  The two instance IDs are DIFFERENT numbers and are easy to conflate:"
    echo "    lokiUser -> grafana.com -> your stack -> Loki  -> Send Logs    -> 'User'"
    echo "    promUser -> grafana.com -> your stack -> Prom  -> Send Metrics -> 'Username'"
    exit 1
  fi
done

if [[ "$(read_cred lokiUser)" == "$(read_cred promUser)" ]]; then
  echo "FAIL: lokiUser and promUser are the same value."
  echo "      They are separate per-signal instance IDs; identical means one was"
  echo "      copied from the wrong panel. Loki pushes would fail with HTTP 530."
  exit 1
fi

echo "==> writing Secret ${SECRET} in ${NAMESPACE}"
kubectl create secret generic "$SECRET" -n "$NAMESPACE" \
  --from-literal=lokiUrl="$(read_cred lokiUrl)" \
  --from-literal=lokiUser="$(read_cred lokiUser)" \
  --from-literal=promUrl="$(read_cred promUrl)" \
  --from-literal=promUser="$(read_cred promUser)" \
  --from-literal=token="$(read_cred token)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "==> helm repo add grafana"
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null

echo "==> helm upgrade --install ${RELEASE} ${CHART} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES"

kubectl -n "$NAMESPACE" rollout status daemonset/"${RELEASE}" --timeout=180s

# Bad credentials do not stop the pod — Alloy starts cleanly, tails happily, and
# simply fails every push, so "Running" proves nothing and neither does the log
# (a wrong tenant ID produces HTTP 530, which says nothing about credentials).
#
# Ask Alloy's own counters instead. sent_bytes_total > 0 is the only statement
# that actually means "logs are landing in Grafana Cloud".
echo "==> verifying delivery (Alloy's own counters, 30s)"
sleep 30
AP="$(kubectl -n "$NAMESPACE" get pods -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)"
M="$(kubectl run "alloy-verify-$$" -n "$NAMESPACE" --rm -i --restart=Never --quiet \
      --image=curlimages/curl:8.11.1 --command -- \
      curl -s --max-time 15 "http://${AP}:12345/metrics" 2>/dev/null || true)"

SENT="$(  awk -F' ' '/^loki_write_sent_bytes_total/{s+=$2} END{print s+0}'    <<<"$M")"
DROPPED="$(awk -F' ' '/^loki_write_dropped_entries_total/{s+=$2} END{print s+0}' <<<"$M")"
CODES="$(grep -o 'status_code="[0-9]*"' <<<"$M" | sort -u | tr '\n' ' ')"

echo "    loki sent_bytes=${SENT}  dropped_entries=${DROPPED}"
echo "    push status codes seen: ${CODES:-none yet}"

if [[ "${SENT:-0}" -gt 0 ]]; then
  echo "    ok: logs are reaching Grafana Cloud"
else
  echo
  echo "    NOT DELIVERING. Nothing has been accepted. Common causes:"
  echo "      530 -> lokiUser is the wrong instance ID (it is NOT the same"
  echo "             number as promUser, and NOT the account/org ID)"
  echo "      401/403 -> token lacks logs:write, or is from another stack"
  echo "    Fix values.local.yaml and re-run; Alloy retries automatically."
  exit 1
fi
cat <<EOF

Verify in Grafana Cloud -> Explore:

    {cluster="home-k3s", app="traefik"} | json | DownstreamStatus >= 400

and that a pod with NO external ingress is absent:

    {cluster="home-k3s", namespace="default"}     # talaria workers: expect nothing

Watch the monthly projection at grafana.com -> your stack -> Billing/Usage.
Budget is 50 GB/month; this allowlist should land near 0.3-1 GB.
EOF
