#!/usr/bin/env bash
# Deploy the private archive reader. There are no secret values in this chart:
# identity is delegated to the existing Authelia service, and the app itself
# holds no credentials. The NAS catalog and recovered media are mounted through
# the PVC rather than injected through Helm.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE="${RELEASE:-early-internet-archive}"
NAMESPACE="${NAMESPACE:-life}"
VALUES="${HERE}/values.yaml"
K="kubectl -n ${NAMESPACE}"

command -v helm >/dev/null || { echo "helm required" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required" >&2; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# The catalog is external state, so no manifest checksum changes when it is
# rebuilt. A new revision rolls the Deployment and makes every init container
# copy the current SQLite file into its own local emptyDir.
CATALOG_REVISION="$(date -u +%Y%m%dT%H%M%SZ)"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  -f "$VALUES" --set-string "catalogCopy.revision=${CATALOG_REVISION}" \
  --atomic --cleanup-on-fail "$@"

echo "==> Rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Archive mount"
CLAIM="$($K get deployment "$RELEASE" -o jsonpath='{.spec.template.spec.volumes[?(@.name=="archive")].persistentVolumeClaim.claimName}')"
$K get pvc "$CLAIM"

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="$RELEASE"

# A request without an Authelia session must not reach the application. Warn
# rather than fail when this machine is off-tailnet and cannot route DuckDNS.
HOST="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || true)"
if [[ -n "$HOST" ]] && command -v curl >/dev/null; then
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://${HOST}/healthz" || true)"
  [[ -n "$CODE" ]] || CODE=000
  case "$CODE" in
    3*)  echo "    ok: https://${HOST}/healthz -> ${CODE} (Authelia)" ;;
    000) echo "    WARN: ${HOST} is unreachable (expected off-tailnet)" ;;
    200) echo "    FAIL: unauthenticated /healthz returned 200; forward-auth is not active" >&2; exit 1 ;;
    *)   echo "    FAIL: unauthenticated /healthz returned ${CODE}, expected a redirect" >&2; exit 1 ;;
  esac
fi
