#!/usr/bin/env bash
# Install/upgrade Grafana Alloy from the upstream chart, with our values.
#
# Values-only project (like infra/headlamp and minecraft/): there is no chart
# here, so this adds the upstream repo first. It also builds the Grafana Cloud
# credentials Secret out of values.local.yaml, because the upstream chart has no
# way to create one and the token must not end up in a ConfigMap.
#
# TWO RELEASES from this one directory, the way infra/democratic-csi does:
#
#   alloy        values.yaml        DaemonSet — logs + every pod-local scrape
#   alloy-probe  values-probe.yaml  Deployment, 1 replica — blackbox probes
#
# The split is not cosmetic: probe targets are static URLs with no node to be
# local to, so on a DaemonSet all nine pods would probe all sixteen hostnames.
# See values-probe.yaml.
#
# Nothing here touches ingress. Alloy only reads.

set -euo pipefail

RELEASE="${RELEASE:-alloy}"
PROBE_RELEASE="${PROBE_RELEASE:-alloy-probe}"
NAMESPACE="${NAMESPACE:-alloy}"
CHART="${CHART:-grafana/alloy}"
# Pin the chart — a surprise bump to the cluster-wide log shipper is an outage,
# not an upgrade. Bump deliberately, reading the chart changelog.
# renovate: datasource=helm depName=alloy registryUrl=https://grafana.github.io/helm-charts
CHART_VERSION="${CHART_VERSION:-1.11.1}"
SECRET="alloy-grafana-cloud"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
PROBE_VALUES="${HERE}/values-probe.yaml"
# The five Grafana Cloud credentials come from 1Password into memory and never
# onto a disk. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "no Grafana Cloud credentials resolved from 1Password"
  echo "  check with:  ./scripts/secrets.sh check infra/alloy"
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# Pull the five credential fields out of values.local.yaml. They go into a
# Secret rather than into the chart values so they never land in the rendered
# ConfigMap, which is world-readable to anyone with namespace access.
# Reads the document off stdin rather than a path: this is called eight times
# below, and each call gets its own fresh pipe from sv_fd.
read_cred() {
  sv_fd | python3 -c "
import sys, yaml
d = yaml.safe_load(sys.stdin) or {}
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
    echo "FAIL: grafanaCloud.${f} is unset or still an example placeholder in the vault item"
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
# --from-file, NOT --from-literal: argv is world-readable in /proc for as long
# as kubectl runs, so --from-literal published all five credentials — including
# the Grafana Cloud token — to every user on the box. --from-file takes the key
# from the basename, so the Secret's shape is unchanged. The directory is in the
# same tmpfs as everything else this run and goes away with it.
# printf '%s' "$(...)", not a plain redirect. read_cred ends in python's print(),
# so a direct redirect writes "value\n" and --from-file keeps that newline INSIDE
# the secret — where --from-literal stripped it via command substitution. A
# Grafana Cloud token with a trailing newline authenticates against nothing, and
# the failure surfaces as HTTP 530 from the push endpoint rather than as
# anything that mentions whitespace.
CRED_DIR="$(sv_scratch grafana-cloud)"
for f in lokiUrl lokiUser promUrl promUser token; do
  ( umask 077; printf '%s' "$(read_cred "$f")" > "${CRED_DIR}/${f}" )
done
kubectl create secret generic "$SECRET" -n "$NAMESPACE" \
  --from-file="${CRED_DIR}/lokiUrl" \
  --from-file="${CRED_DIR}/lokiUser" \
  --from-file="${CRED_DIR}/promUrl" \
  --from-file="${CRED_DIR}/promUser" \
  --from-file="${CRED_DIR}/token" \
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
#
# Both releases go through this, in one throwaway pod — starting a pod per
# release doubles the slowest part of the script for no extra safety.
VPOD="alloy-validate-$$"
VALIDATE_STARTED=0

start_validator() {
  local image
  image="$(helm template "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES" | python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') in ('DaemonSet', 'Deployment'):
        print(d['spec']['template']['spec']['containers'][0]['image']); break
")"
  [[ -n "$image" ]] || { echo "FAIL: could not determine the Alloy image"; exit 1; }

  kubectl -n "$NAMESPACE" delete pod "$VPOD" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" run "$VPOD" --image="$image" --restart=Never \
    --command -- sleep 300 >/dev/null
  for _ in $(seq 1 40); do
    sleep 3
    [[ "$(kubectl -n "$NAMESPACE" get pod "$VPOD" -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && break
  done
  VALIDATE_STARTED=1
}

cleanup_validator() {
  [[ "$VALIDATE_STARTED" -eq 1 ]] || return 0
  kubectl -n "$NAMESPACE" delete pod "$VPOD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup_validator EXIT

# validate_config <release> <values-file>
validate_config() {
  local rel="$1" vals="$2" cfg out
  cfg="$(helm template "$rel" "$CHART" -n "$NAMESPACE" -f "$vals" | python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'ConfigMap':
        for k, v in (d.get('data') or {}).items():
            if k.endswith('.alloy'):
                sys.stdout.write(v)
")"
  if [[ -z "$cfg" ]]; then
    echo "FAIL: could not find the rendered Alloy config for ${rel}."
    exit 1
  fi

  printf '%s' "$cfg" | kubectl -n "$NAMESPACE" exec -i "$VPOD" -- sh -c 'cat > /tmp/config.alloy'
  if ! out="$(kubectl -n "$NAMESPACE" exec "$VPOD" -- alloy validate /tmp/config.alloy 2>&1)"; then
    echo "FAIL: Alloy rejected the rendered config for ${rel}. Nothing has been applied."
    sed 's/^/      /' <<<"$out" | tail -25
    exit 1
  fi
  echo "    ok: ${rel} config graph resolves"
}

echo "==> Validating the rendered Alloy configs"
start_validator
validate_config "$RELEASE" "$VALUES"
validate_config "$PROBE_RELEASE" "$PROBE_VALUES"
cleanup_validator
VALIDATE_STARTED=0

echo "==> helm upgrade --install ${RELEASE} ${CHART}@${CHART_VERSION} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$CHART" --version "$CHART_VERSION" -n "$NAMESPACE" -f "$VALUES" --cleanup-on-fail

echo "==> helm upgrade --install ${PROBE_RELEASE} ${CHART}@${CHART_VERSION} -n ${NAMESPACE}"
helm upgrade --install "$PROBE_RELEASE" "$CHART" --version "$CHART_VERSION" -n "$NAMESPACE" -f "$PROBE_VALUES" --cleanup-on-fail

# The credentials arrive as env vars from a Secret, and env vars are read once
# at process start. Rewriting the Secret alone leaves every running pod using
# the old values, so a re-run after fixing a wrong instance ID would appear to
# change nothing. Always restart.
if kubectl -n "$NAMESPACE" get daemonset "$RELEASE" >/dev/null 2>&1; then
  echo "==> restarting so the credential Secret is re-read"
  kubectl -n "$NAMESPACE" rollout restart daemonset/"$RELEASE" >/dev/null
fi
if kubectl -n "$NAMESPACE" get deployment "$PROBE_RELEASE" >/dev/null 2>&1; then
  kubectl -n "$NAMESPACE" rollout restart deployment/"$PROBE_RELEASE" >/dev/null
fi

# Ten nodes rolled one at a time, each waiting on a readiness probe: the old
# 300s was tight enough that a healthy deploy timed out at 9/10 and skipped
# every check below it.
kubectl -n "$NAMESPACE" rollout status daemonset/"${RELEASE}" --timeout=900s
kubectl -n "$NAMESPACE" rollout status deployment/"${PROBE_RELEASE}" --timeout=300s

# The locality filter in values.yaml is only correct if this DaemonSet actually
# reaches every node — a pod missing from a node means that node's logs and
# metrics are silently dropped rather than picked up by a neighbour. That
# depends entirely on controller.tolerations, which is one careless edit away
# from being removed, so assert it here rather than trusting it.
NODES="$(kubectl get nodes --no-headers | wc -l)"
SCHED="$(kubectl -n "$NAMESPACE" get daemonset "$RELEASE" -o jsonpath='{.status.desiredNumberScheduled}')"
echo "==> node coverage: ${SCHED}/${NODES}"
if [[ "$SCHED" != "$NODES" ]]; then
  echo
  echo "FAIL: Alloy is scheduled on ${SCHED} of ${NODES} nodes."
  echo "      Every scrape and log tier filters to the LOCAL node, so the"
  echo "      node(s) without a pod are now a silent blind spot — not covered"
  echo "      by anyone. Check controller.tolerations in values.yaml against:"
  kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints --no-headers | sed 's/^/        /'
  exit 1
fi

# Bad credentials do not stop the pod — Alloy starts cleanly, tails happily, and
# simply fails every push, so "Running" proves nothing and neither does the log
# (a wrong tenant ID produces HTTP 530, which says nothing about credentials).
#
# Ask Alloy's own counters instead. sent_bytes_total > 0 is the only statement
# that actually means "logs are landing in Grafana Cloud".
#
# ⚠️  THIS MUST SUM ACROSS EVERY POD, not sample one.
#
# It used to read `.items[0]` and that was fine when all nine pods shipped all
# the same logs. With the locality filter it is wrong: a pod ships only what is
# scheduled beside it, so an Alloy on a node running no Traefik, no Authelia, no
# cloudflared, no egress-proxy and a quiet CrowdSec agent legitimately reports
# sent_bytes=0 forever. Measured right after this change: one pod at 0, one at
# 3.4 MB, the rest scattered between. Sampling one pod would fail a perfectly
# healthy deploy roughly one time in ten, at random.
echo "==> verifying delivery (Alloy's own counters, 30s)"
sleep 30
IPS="$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/instance="$RELEASE" \
       -o jsonpath='{range .items[*]}{.status.podIP} {end}' 2>/dev/null)"
M="$(kubectl run "alloy-verify-$$" -n "$NAMESPACE" --rm -i --restart=Never --quiet \
      --image=curlimages/curl:8.11.1 --command -- \
      sh -c "for ip in ${IPS}; do curl -s --max-time 10 http://\$ip:12345/metrics; done" 2>/dev/null || true)"

SENT="$(  awk -F' ' '/^loki_write_sent_bytes_total/{s+=$2} END{print s+0}'    <<<"$M")"
DROPPED="$(awk -F' ' '/^loki_write_dropped_entries_total/{s+=$2} END{print s+0}' <<<"$M")"
SHIPPERS="$(awk -F' ' '/^loki_write_sent_bytes_total/{if ($2+0 > 0) n++} END{print n+0}' <<<"$M")"
CODES="$(grep -o 'status_code="[0-9]*"' <<<"$M" | sort -u | tr '\n' ' ')"
echo "    ${SHIPPERS} of ${SCHED} pods have shipped something"

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
