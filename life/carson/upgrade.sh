#!/usr/bin/env bash
# Apply the chart.
#
# Extra arguments pass straight through to `helm upgrade`, which is how to run
# a smoke test without disturbing real state, e.g. aiming a reminder sweep at a
# date that actually has something on it:
#
#   ./upgrade.sh --set extraEnv.CARSON_TODAY=2026-09-03

set -euo pipefail

RELEASE="${RELEASE:-carson}"
NAMESPACE="${NAMESPACE:-life}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

# The sms-relay API key, the calendar feed token and the recipient number all
# live at op://homelab/life-carson/values.local.yaml. Resolved into memory and
# passed by process substitution — none of it touches a disk.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "FAIL: no values resolved from 1Password — this holds the sms-relay key,"
  echo "      the feed token and the recipient number."
  echo "      check with:  ./scripts/secrets.sh check life/carson"
  exit 1
fi

# Refuse to roll onto a tag that isn't in the registry. The Deployment would
# ImagePullBackOff (visible), but the CronJob would fail silently at 08:00
# tomorrow — which is exactly the failure a reminder system must not have.
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
kubectl -n "$NAMESPACE" get cronjob "${RELEASE}-reminders"

# The feed token is a live credential — it is the ONLY thing standing in front
# of every birthday and note in the database. Printing the whole URL puts it in
# scrollback, which on the claude-workspace pod is a tmux buffer on a PVC. So it
# is elided unless explicitly asked for:
#
#     CARSON_SHOW_FEED_URL=1 ./upgrade.sh
#
# You need it exactly twice — once per calendar — and `secrets.sh show
# life/carson --reveal` can produce it again without a redeploy.
if [[ -n "${CARSON_SHOW_FEED_URL:-}" ]]; then
  FEED_TOKEN="$(sv_fd | awk '/^  feedToken:/{print $2}' | tr -d '"' || true)"
else
  FEED_TOKEN=""
fi
cat <<MSG

Dashboard:  https://$(awk '/^  host:/{print $2; exit}' "$VALUES")/   (Authelia)
Feed:       https://$(awk '/^  host:/{print $2; exit}' "$VALUES")/feed/${FEED_TOKEN:-<token — CARSON_SHOW_FEED_URL=1 to print>}.ics

Subscribe that feed URL in BOTH calendars — it is the only way carson's dates
reach a device:
  iOS      Calendar -> Calendars -> Add Calendar -> Add Subscription Calendar
  Google   Other calendars -> + -> From URL

To run a reminder sweep now instead of waiting for 08:00:
    kubectl -n ${NAMESPACE} create job --from=cronjob/${RELEASE}-reminders carson-manual-\$(date +%s)
    kubectl -n ${NAMESPACE} logs -f job/carson-manual-...

Nothing due means no text, by design. The log line is "reminder sweep complete: N sent".
MSG
