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
#
# Subresources go through SubjectAccessReview, NOT `kubectl auth can-i`. can-i
# parses `nodes/proxy` as resource=nodes NAME=proxy, so it answers "yes" for
# anyone who can get any node — which is every read this chart needs. Using it
# here would fail every deploy on a permission hatch does not actually hold.
echo "==> RBAC check"
SA="system:serviceaccount:${NAMESPACE}:${RELEASE}"
rbac_fail=0

# verb|resource|subresource|expected
while IFS='|' read -r verb res sub expect; do
  [[ -n "$verb" ]] || continue
  allowed=$(kubectl create -o jsonpath='{.status.allowed}' -f - <<YAML 2>/dev/null
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: ${SA}
  resourceAttributes:
    verb: ${verb}
    resource: ${res}
    subresource: "${sub}"
YAML
)
  label="${verb} ${res}${sub:+/$sub}"
  if [[ "$allowed" != "$expect" ]]; then
    echo "    FAIL: ${label} -> allowed=${allowed:-unknown}, expected ${expect}"
    rbac_fail=1
  else
    echo "    ok: ${label} allowed=${allowed}"
  fi
done <<'PROBES'
get|secrets||false
get|configmaps||false
create|pods|exec|false
create|pods|portforward|false
get|nodes|proxy|false
get|nodes|stats|false
create|pods||false
get|pods|log|true
list|pods||true
list|nodes||true
PROBES

[[ $rbac_fail -eq 0 ]] || { echo "FAIL: RBAC is not what the chart intends"; exit 1; }

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
