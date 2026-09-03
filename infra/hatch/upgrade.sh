#!/usr/bin/env bash
# Apply the chart.
set -euo pipefail

RELEASE="${RELEASE:-hatch}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "FAIL: no values resolved from 1Password."
  echo "      hatch will not serve without an API key — it exits at startup instead"
  echo "      of answering an unauthenticated cluster API, so a keyless deploy is a"
  echo "      CrashLoop, not a security hole. Fix the vault item first:"
  echo "        ./scripts/secrets.sh check infra/hatch"
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE} $*"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  -f "$VALUES" -f <(sv_fd) "$@" --cleanup-on-fail

echo "==> Rollout"
kubectl -n "$NAMESPACE" rollout status deploy/"$RELEASE" --timeout=180s

# Assert the negative space. The whole design rests on hatch NOT holding these,
# and a rendering mistake in rbac.yaml would be silent otherwise.
echo "==> RBAC check"
SA="system:serviceaccount:${NAMESPACE}:${RELEASE}"
rbac_fail=0
for probe in "get secrets" "create pods/exec" "get nodes/proxy" "create pods"; do
  # shellcheck disable=SC2086  # deliberate word splitting: verb + resource
  if kubectl auth can-i $probe --all-namespaces --as="$SA" >/dev/null 2>&1; then
    echo "    FAIL: ${RELEASE} can '${probe}' — it must not"
    rbac_fail=1
  else
    echo "    ok: cannot ${probe}"
  fi
done
[[ $rbac_fail -eq 0 ]] || { echo "FAIL: RBAC is wider than intended"; exit 1; }

ACTIONS="$(helm get values "$RELEASE" -n "$NAMESPACE" --all -o json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["actions"]["enabled"])' 2>/dev/null || echo unknown)"
echo "==> actions.enabled=${ACTIONS}"

# The endpoint, not just the rollout: a Ready pod behind a broken Ingress still
# leaves the agent with nothing.
HOST="$(kubectl -n "$NAMESPACE" get ingress "$RELEASE" \
  -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || true)"
if [[ -n "$HOST" ]]; then
  echo "==> Probing https://${HOST}/healthz"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://${HOST}/healthz" || echo 000)"
  if [[ "$code" == "200" ]]; then
    echo "    ok: 200"
  else
    echo "    WARN: got HTTP ${code} (tailnet-only host — expected if you are off the tailnet)"
  fi
  echo "==> Confirming an unauthenticated read is refused"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://${HOST}/v1/nodes" || echo 000)"
  [[ "$code" == "401" ]] && echo "    ok: 401" || echo "    WARN: expected 401, got ${code}"
fi
