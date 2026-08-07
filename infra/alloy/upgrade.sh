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

# Materialize values.local.yaml from 1Password when it's missing and a template
# exists. Convenience only: values.local.yaml is still the contract, so this
# no-ops without `op` — e.g. in the claude-workspace pod, which is fed by
# `scripts/secrets.sh publish` instead. See values.local.tpl.yaml.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi


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

# ---------------------------------------------------------------------------
# Does the rendered config actually parse?
# ---------------------------------------------------------------------------
# This is a DaemonSet on every node, and the config is a single inline River
# document that is easy to get subtly wrong — a renamed component leaves a
# dangling reference that looks fine in YAML and fails only when Alloy starts.
# The rollout below would catch it (a crashlooping pod stalls the rollout rather
# than sweeping the fleet), but it catches it *after* taking a node's shipper
# down, and the error is then buried in a pod log rather than printed here.
#
# `alloy validate` resolves the component graph, so it catches exactly that
# class of mistake. Verified to fail (exit 1) on a dangling reference.
echo "==> Validating the rendered Alloy config"
RENDERED_CFG="$(helm template "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES" | python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'ConfigMap':
        for k, v in (d.get('data') or {}).items():
            if k.endswith('.alloy'):
                sys.stdout.write(v)
")"
VALIDATE_IMAGE="$(helm template "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES" | python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'DaemonSet':
        print(d['spec']['template']['spec']['containers'][0]['image']); break
")"

if [[ -z "$RENDERED_CFG" ]]; then
  echo "FAIL: could not find the rendered Alloy config in the chart output."
  exit 1
fi

VPOD="alloy-validate-$$"
kubectl -n "$NAMESPACE" delete pod "$VPOD" --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" run "$VPOD" --image="$VALIDATE_IMAGE" --restart=Never \
  --command -- sleep 180 >/dev/null
for _ in $(seq 1 40); do
  sleep 3
  [[ "$(kubectl -n "$NAMESPACE" get pod "$VPOD" -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && break
done
printf '%s' "$RENDERED_CFG" | kubectl -n "$NAMESPACE" exec -i "$VPOD" -- sh -c 'cat > /tmp/config.alloy'
if ! VOUT="$(kubectl -n "$NAMESPACE" exec "$VPOD" -- alloy validate /tmp/config.alloy 2>&1)"; then
  echo "FAIL: Alloy rejected the rendered config. Nothing has been applied."
  sed 's/^/      /' <<<"$VOUT" | tail -25
  kubectl -n "$NAMESPACE" delete pod "$VPOD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  exit 1
fi
kubectl -n "$NAMESPACE" delete pod "$VPOD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
echo "    ok: config graph resolves"

echo "==> helm upgrade --install ${RELEASE} ${CHART} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES"

# The credentials arrive as env vars from a Secret, and env vars are read once
# at process start. Rewriting the Secret alone leaves every running pod using
# the old values, so a re-run after fixing a wrong instance ID would appear to
# change nothing. Always restart.
if kubectl -n "$NAMESPACE" get daemonset "$RELEASE" >/dev/null 2>&1; then
  echo "==> restarting so the credential Secret is re-read"
  kubectl -n "$NAMESPACE" rollout restart daemonset/"$RELEASE" >/dev/null
fi

kubectl -n "$NAMESPACE" rollout status daemonset/"${RELEASE}" --timeout=300s

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
