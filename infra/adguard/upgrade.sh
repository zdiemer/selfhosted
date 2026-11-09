#!/usr/bin/env bash
# Apply the chart, then prove the two things that matter from a client's side:
#
#   1. DNS on a node's tailnet address resolves real names AND blocks ads.
#      Checked against every address in CHECK_NODES (the ones Tailscale is
#      told to use), because one node's svclb can be broken while the others
#      work.
#   2. The admin UI is not open. AdGuard runs with no users of its own, so an
#      anonymous request that gets a 200 means the Authelia middleware is
#      missing and anyone on the tailnet can turn filtering off.
#
# No secrets: there's no values.local.tpl.yaml for this chart.

set -euo pipefail

RELEASE="${RELEASE:-adguard}"
NAMESPACE="${NAMESPACE:-dns}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# The two node addresses set as Tailscale global nameservers (README).
CHECK_NODES="${CHECK_NODES:-100.71.229.116 100.80.231.119}"
K="kubectl -n ${NAMESPACE}"

# PRECONDITION, not checked here: every node must have Tailscale DNS off
# (infra/node-configuration/apply.py --apply). Otherwise, once the tailnet
# points at this resolver, the nodes resolve through it too, and the cluster
# then depends on its own DNS to pull images and run CoreDNS.

for c in helm kubectl dig curl; do command -v "$c" >/dev/null || { echo "$c required"; exit 1; }; done

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  kubectl create namespace "$NAMESPACE"
  # Label it before any pod exists (infra/pod-security/README.md).
  python3 "${HERE}/../pod-security/apply.py" --apply
fi

# Phase 1: pods only, no :53 LoadBalancer. If the pods can't come up (image
# pull, config, policy), nothing has claimed :53 on any node yet, so a failure
# here breaks nobody's DNS. On an upgrade of a running release, phase 1 keeps
# the existing LoadBalancer: removing it would cut DNS for the length of the
# rollout.
LB_PHASE1=false
if $K get service "${RELEASE}-dns" >/dev/null 2>&1; then LB_PHASE1=true; fi

echo "==> Phase 1: helm upgrade --install ${RELEASE} (dns.loadBalancer=${LB_PHASE1})"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "${HERE}/values.yaml" \
  --set dns.loadBalancer="${LB_PHASE1}" --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
if ! $K rollout status "deployment/${RELEASE}" --timeout=300s; then
  echo "!! pods never became Ready. The :53 LoadBalancer was NOT created."
  $K get pods -o wide
  exit 1
fi

# Phase 2: the pods answer, so now expose :53 on the nodes.
if [[ "$LB_PHASE1" != true ]]; then
  echo "==> Phase 2: enabling the :53 LoadBalancer"
  helm upgrade "$RELEASE" "$HERE" -n "$NAMESPACE" -f "${HERE}/values.yaml" --cleanup-on-fail
  # svclb pods need a moment to start on every node.
  kubectl -n kube-system rollout status "daemonset/$(kubectl -n kube-system get ds -o name | grep "svclb-${RELEASE}-dns" | cut -d/ -f2)" --timeout=120s
fi

fail=0
for ip in $CHECK_NODES; do
  echo "==> DNS via ${ip}"
  real="$(dig +short +time=3 +tries=2 @"$ip" example.com A | head -1 || true)"
  ad="$(dig +time=3 +tries=2 @"$ip" pagead2.googlesyndication.com A | grep -o 'status: [A-Z]*' | cut -d' ' -f2 || true)"
  if [[ -z "$real" || "$real" == "0.0.0.0" ]]; then
    echo "    !! example.com -> '${real}' (expected a real address)"; fail=1
  else
    echo "    example.com -> ${real}"
  fi
  if [[ "$ad" == "NXDOMAIN" ]]; then
    echo "    pagead2.googlesyndication.com -> NXDOMAIN (blocked)"
  else
    echo "    !! pagead2.googlesyndication.com -> '${ad}' (expected NXDOMAIN; filters may still be loading)"; fail=1
  fi
done

HOST="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[0].host}')"
echo "==> Checking https://${HOST} rejects anonymous requests"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "https://${HOST}/control/status" || true)"
case "$CODE" in
  302|401) echo "    anonymous -> ${CODE}, gated" ;;
  *) echo "    !! anonymous -> ${CODE}. The UI may be OPEN (it has no login of its own)"; fail=1 ;;
esac

exit "$fail"
