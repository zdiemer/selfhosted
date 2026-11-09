#!/usr/bin/env bash
# Apply the chart, then check the login from outside, the way a browser sees it:
#
#   - An anonymous request must be bounced to Authelia (302/401). A 200 means
#     the forward-auth middleware is missing. Open WebUI trusts the
#     Remote-Email header without question, so that would let anyone log in
#     as anyone.
#   - A request carrying a forged Remote-Email header must ALSO be bounced,
#     i.e. the header alone is never treated as a login. (Traefik also
#     overwrites it after a successful auth; that half isn't testable without
#     a real session.)
#   - In-cluster, Open WebUI must reach Ollama, which proves the NetworkPolicy
#     pair (ollama ingress + application-isolation egress) lines up.
#
# Ollama's own chart must be upgraded first so its NetworkPolicy admits this
# pod. Run ai/ollama/upgrade.sh before this on a first install.

set -euo pipefail

RELEASE="${RELEASE:-open-webui}"
NAMESPACE="${NAMESPACE:-ai}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

K="kubectl -n ${NAMESPACE}"
command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

# Recreate + a multi-GB image + a first-boot embedding-model download.
echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=900s

HOST="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[0].host}')"
fail=0

echo "==> Anonymous request to https://${HOST}/api/config"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "https://${HOST}/api/config" || true)"
case "$CODE" in
  302|401) echo "    -> ${CODE}, gated" ;;
  *) echo "    !! -> ${CODE}. The host may be OPEN"; fail=1 ;;
esac

echo "==> Forged Remote-Email header"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -H 'Remote-Email: forged@example.com' "https://${HOST}/api/v1/auths/" || true)"
case "$CODE" in
  302|401) echo "    -> ${CODE}, header not trusted from the client" ;;
  *) echo "    !! -> ${CODE}. A client-sent identity header got through"; fail=1 ;;
esac

echo "==> Open WebUI -> Ollama (in-cluster)"
if $K exec "deploy/${RELEASE}" -- python3 -c '
import os, sys, urllib.request
u = os.environ["OLLAMA_BASE_URL"] + "/api/version"
print("    " + urllib.request.urlopen(u, timeout=5).read().decode())
'; then :; else
  echo "    !! cannot reach Ollama. Check ai/ollama's NetworkPolicy and infra/application-isolation"
  fail=1
fi

exit "$fail"
