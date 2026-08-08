#!/usr/bin/env bash
# Text me when a systemd unit fails. Wired up as OnFailure= on the secret and
# submodule timers.
#
# WHY
# scripts/systemd/README.md already makes the argument for the backup timer: a
# scheduled job that fails silently is worse than no job, because you stop
# checking. The sync timer has the same shape — it is the thing that keeps
# 1Password and the working copies converged, and if it stops, nothing else
# notices. So it says so, out loud, once per failure.
#
# Configuration lives in ~/.config/selfhosted/alert.env (0600):
#
#     SMS_RELAY_URL=https://sms-relay.zachd.duckdns.org
#     SMS_RELAY_KEY=<a key from sms-relay's SMS_RELAY_API_KEYS map>
#     ALERT_TO=2025550101
#
# Absent that file this exits 0 after saying so in the journal. Alerting is
# opt-in; a missing config must not turn one failure into two.

set -euo pipefail

UNIT="${1:-unknown.service}"
ENV_FILE="${ALERT_ENV:-$HOME/.config/selfhosted/alert.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "notify-failure: ${UNIT} failed, but ${ENV_FILE} does not exist — no SMS sent" >&2
  exit 0
fi

# shellcheck source=/dev/null
set -a; . "$ENV_FILE"; set +a

: "${SMS_RELAY_URL:?SMS_RELAY_URL missing from ${ENV_FILE}}"
: "${SMS_RELAY_KEY:?SMS_RELAY_KEY missing from ${ENV_FILE}}"
: "${ALERT_TO:?ALERT_TO missing from ${ENV_FILE}}"

# The last few journal lines are what makes the text actionable rather than
# merely alarming. Trimmed hard: this is going to a phone.
DETAIL="$(journalctl --user -u "$UNIT" -n 6 --no-pager -o cat 2>/dev/null | tail -c 400 || true)"
BODY="$(printf '%s failed on %s\n\n%s' "$UNIT" "$(hostname -s)" "${DETAIL:-no journal output}")"

# One text per unit per hour. The sync timer runs every 15 minutes, and a
# persistent failure would otherwise send 96 messages a day — which is exactly
# how an alert becomes something you mute. sms-relay dedupes on
# idempotency_key (a repeat POST returns the original message instead of
# sending), so bucketing the key by hour does the rate limiting server-side,
# with no state to keep here.
IDEM="${UNIT}-$(date -u +%Y%m%dT%H)"

python3 - "$SMS_RELAY_URL" "$SMS_RELAY_KEY" "$ALERT_TO" "$BODY" "$IDEM" <<'PY'
import json, sys, urllib.request, urllib.error
url, key, to, body, idem = sys.argv[1:6]
req = urllib.request.Request(
    url.rstrip("/") + "/api/v1/messages",
    data=json.dumps({"to": to, "body": body, "idempotency_key": idem}).encode(),
    headers={"X-API-Key": key, "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print(f"notify-failure: sms-relay accepted ({r.status})", file=sys.stderr)
except urllib.error.HTTPError as e:
    sys.exit(f"notify-failure: sms-relay returned {e.code}: {e.read()[:200]!r}")
except Exception as e:
    sys.exit(f"notify-failure: could not reach sms-relay: {e}")
PY
