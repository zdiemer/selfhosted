#!/usr/bin/env bash
# Apply the current chart to the running ollama release.
#
# The deploy check is about the gate as much as the server: an Ollama that is
# Running but published without working basic auth is an open compute endpoint
# on the public internet, and one behind a broken gate is a 401 for every
# legitimate client. So this hits the public host twice — once with no
# credential expecting 401, once with the vault credential expecting the
# version — which exercises Traefik, the middleware, the secret and the server
# in one pass. Model availability is reported, not asserted: pulls run in the
# background after Ready, and a first install legitimately has none yet.

set -euo pipefail

RELEASE="${RELEASE:-ollama}"
NAMESPACE="${NAMESPACE:-ai}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Secrets are resolved from 1Password into memory for the life of this run and
# never written to a disk. Each helm call spells out its own `-f <(sv_fd)`:
# that fd is a pipe, readable once, so a shared one would hand the second
# reader an empty values file. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

# Recreate strategy + a ~1.5GB image on first install: the old pod must die and
# the new one pull before anything is Ready, hence the generous timeout.
echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=600s

# Hitting the public URL is the check that matters. A ClusterIP curl passes
# just as happily when the ingress is wrong, the cert is stale, or the
# middleware secret rendered empty and Traefik denies everyone.
BASE="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[0].host}')"

echo "==> Checking the gate rejects anonymous requests"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "https://${BASE}/api/version" || true)"
if [[ "$CODE" == "401" ]]; then
  echo "    anonymous -> 401, good"
else
  echo "    !! expected 401 for anonymous, got ${CODE} - the host may be OPEN"
  exit 1
fi

# The smoke credential comes out of the same in-memory values document helm
# just consumed, so there is no second secret path to drift.
SMOKE="$(sv_fd | python3 -c '
import sys, yaml
v = yaml.safe_load(sys.stdin) or {}
s = (((v.get("auth") or {}).get("basicAuth") or {}).get("smoke") or {})
u, p = s.get("user") or "", s.get("password") or ""
print(f"{u}:{p}" if u and p else "")
')"

if [[ -n "$SMOKE" ]]; then
  echo "==> Checking the credential is accepted"
  if VERSION="$(curl -fsS -u "$SMOKE" "https://${BASE}/api/version")"; then
    echo "    authenticated -> ${VERSION}"
  else
    echo "    !! authenticated request FAILED - the gate is up but nobody can pass it"
    exit 1
  fi
  echo "==> Models on the volume (pulls continue in the background)"
  curl -fsS -u "$SMOKE" "https://${BASE}/api/tags" | python3 -c '
import sys, json
ms = json.load(sys.stdin).get("models") or []
lines = ["    %s  %.1fGB" % (m["name"], m["size"] / 1e9) for m in ms]
print("\n".join(lines) or "    (none yet - first pull still running, watch the pod logs)")
'
else
  echo "==> Skipping the credential check (no auth.basicAuth.smoke in the vault values)"
fi

echo "==> Ingress"
$K get ingress "$RELEASE"
