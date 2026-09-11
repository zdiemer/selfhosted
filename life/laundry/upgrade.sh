#!/usr/bin/env bash
# Apply the chart.
#
# Extra arguments pass straight through to `helm upgrade`, which is how tuning
# is done — no rebuild, no reflash, just new numbers:
#
#   ./upgrade.sh --set machines[0].quietSeconds=600
#   ./upgrade.sh --set extraEnv.LAUNDRY_DRY_RUN=1     # log texts, send none

set -euo pipefail

RELEASE="${RELEASE:-laundry}"
NAMESPACE="${NAMESPACE:-life}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

# The sms-relay API key, the device ingest token and the recipient number all
# live at op://homelab/life-laundry/values.local.yaml. Resolved into memory and
# passed by process substitution — none of it touches a disk.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "FAIL: no values resolved from 1Password — this holds the sms-relay key,"
  echo "      the device ingest token and the recipient number."
  echo "      check with:  ./scripts/secrets.sh check life/laundry"
  exit 1
fi

# Refuse to roll onto a tag that isn't in the registry. `strategy: Recreate`
# means a typo is an outage rather than a failed rollout — and an outage here
# is silent, because a laundry monitor that is down looks exactly like a
# laundry room where nobody is doing laundry.
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
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  -f "$VALUES" -f <(sv_fd) "$@" --cleanup-on-fail

echo "==> Rollout"
kubectl -n "$NAMESPACE" rollout status deploy/"$RELEASE" --timeout=120s

INGEST_PORT="$(awk '/^ingestService:/{f=1} f&&/^  port:/{print $2; exit}' "$VALUES")"
cat <<MSG

Dashboard:  https://$(awk '/^  host:/{print $2; exit}' "$VALUES")/   (Authelia)

Sensors post to any node IP on :${INGEST_PORT} — put one of these in the
firmware's include/config.h as LAUNDRY_HOST:

$(kubectl get nodes -o jsonpath='{range .items[*]}    {.metadata.name}{"\t"}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' 2>/dev/null)

Check what the sensors are actually reporting:
    kubectl -n ${NAMESPACE} logs -f deploy/${RELEASE}

Test the whole SMS path without doing a load — this sends a real text:
    kubectl -n ${NAMESPACE} port-forward deploy/${RELEASE} 8080:8080
    curl -X POST 'localhost:8080/api/v1/test-notify?device=washer'

Nothing running means no text, by design. The log line for a completed load is
"CYCLE COMPLETE after ... — texting".
MSG
