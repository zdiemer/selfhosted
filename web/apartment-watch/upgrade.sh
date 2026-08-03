#!/usr/bin/env bash
# Apply the chart. Also the way to ship an edited criteria.yaml — that's a
# ConfigMap, so it needs no rebuild, just this.
#
# Extra arguments are passed straight through to `helm upgrade`, which is how
# you run a smoke test without disturbing real state, e.g.:
#
#   ./upgrade.sh --set extraEnv.APARTMENT_WATCH_DB=/data/smoke-test.db
#
# pointing a throwaway run at a throwaway database so it can't mark real
# listings as already-notified.

set -euo pipefail

RELEASE="${RELEASE:-apartment-watch}"
NAMESPACE="${NAMESPACE:-web}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Both local files are gitignored, so a fresh clone has neither. Say which one
# is missing instead of letting helm render an empty secret or an empty config.
if [[ ! -f "${HERE}/criteria.yaml" ]]; then
  echo "FAIL: criteria.yaml is missing (it's gitignored — it holds your phone"
  echo "      number and neighbourhood list)."
  echo "      cp ${HERE}/criteria.example.yaml ${HERE}/criteria.yaml"
  exit 1
fi
if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "FAIL: values.local.yaml is missing — it holds the sms-relay API key."
  echo "      cp ${HERE}/values.local.yaml.example ${LOCAL_VALUES}"
  exit 1
fi

# Fail before deploying if criteria.yaml won't parse. The CronJob validates it
# too, but there the feedback arrives at 09:00 tomorrow.
if command -v python3 >/dev/null; then
  echo "==> Validating criteria.yaml"
  python3 -c "
import sys
sys.path.insert(0, '${HERE}/src')
import config
c = config.load('${HERE}/criteria.yaml')
print(f'    ok: <= \${c.search.max_effective_rent}, {c.search.min_bedrooms}-{c.search.max_bedrooms}br, '
      f'excluding {len(c.exclude_neighborhoods)} neighbourhoods, sources: {\", \".join(c.enabled_sources)}')
" || { echo "criteria.yaml is invalid — fix it before deploying"; exit 1; }
fi

# Refuse to roll onto a tag that isn't in the registry. A CronJob fails softer
# than a Deployment (the old pod isn't torn down, there just isn't one), but a
# typo here means you silently get no alerts until you notice — which is exactly
# the failure this tool is supposed to not have.
REPO="$(awk '/^  repository:/{print $2; exit}' "$VALUES" | tr -d '"')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "$VALUES")"
if [[ "$REPO" == ghcr.io/* && -n "$TAG" ]] && command -v curl >/dev/null && command -v python3 >/dev/null; then
  echo "==> Verifying ${REPO}:${TAG} exists"
  IMG_PATH="${REPO#ghcr.io/}"
  BASIC="$(python3 - "$HOME/.docker/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("auths", {}).get("ghcr.io", {}).get("auth", ""))
except Exception:
    print("")
PY
)"
  TOKEN="$(curl -fsSL ${BASIC:+-H "Authorization: Basic ${BASIC}"} \
           "https://ghcr.io/token?scope=repository:${IMG_PATH}:pull&service=ghcr.io" \
           | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
  if [[ -z "$TOKEN" ]]; then
    echo "    skipped (no GHCR credentials — cannot verify)" >&2
  else
    CODE="$(curl -sL -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" \
            -H 'Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json' \
            "https://ghcr.io/v2/${IMG_PATH}/manifests/${TAG}" || echo 000)"
    if [[ "$CODE" != "200" ]]; then
      echo "ERROR: ${REPO}:${TAG} is not in the registry (HTTP ${CODE})." >&2
      echo "       Run build.sh first." >&2
      exit 1
    fi
    echo "    ok"
  fi
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE} $*"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" "$@"

echo "==> CronJob"
$K get cronjob "$RELEASE"

cat <<EOF

Next run is on the schedule above. To run it now:

    kubectl -n ${NAMESPACE} create job --from=cronjob/${RELEASE} aw-manual-\$(date +%s)
    kubectl -n ${NAMESPACE} logs -f job/aw-manual-...

No matches means no text, by design. The log line to look for is either
"queued sms" or "no new matches — staying quiet".
EOF
