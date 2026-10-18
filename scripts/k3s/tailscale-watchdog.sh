#!/bin/bash
# Restart an unresponsive daemon, but never interrupt login or node approval.
# Install as /usr/local/bin/tailscale-watchdog on hosts using that systemd timer.
status_json=$(timeout 15 tailscale status --json 2>/dev/null) || true
state=$(printf '%s' "$status_json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("BackendState", ""))
except (ValueError, AttributeError):
    pass
')
case "$state" in
    Running|Starting|NeedsLogin|NeedsMachineAuth|Stopped|NoState)
        # A valid backend state proves the daemon is responsive. Authentication
        # and administrative states need their own action, not a restart loop.
        exit 0
        ;;
esac
logger -t tailscale-watchdog "Tailscale daemon unresponsive or invalid status; restarting tailscaled"
systemctl restart tailscaled
