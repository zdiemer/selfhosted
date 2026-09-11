#!/usr/bin/env bash
# Dead-man's switch for the Robinhood trading agent.
#
# The agent runs in the claude-workspace pod (dev/claude-workspace,
# messaging.schedules "trading-hourly"/"trading-close") and ends every run —
# including no-op ones — by committing and pushing its journal to
# github.com/zdiemer/trading-journal. So "the journal moved recently" is the
# one signal that covers the whole chain: pod up, gateway up, schedule armed,
# claude ran, git push worked. This script fails when that signal goes stale,
# and the failure texts through OnFailure=selfhosted-alert@%n.service — the
# exact alarm that did not exist when the previous incarnation (a tmux session
# on this machine) died on 2026-08-05 and nobody noticed for three weeks.
#
# Runs from a systemd user timer Mon–Fri evenings (see
# scripts/systemd/selfhosted-trading-watchdog.timer). Thresholds: the agent
# pushes every hour of the session (9:45–14:45 at :45, plus 15:30 ET), so on
# Tue–Fri anything older than MAX_AGE_HOURS is wrong; Monday allows the
# weekend gap since Friday's 15:30 ET run. The thresholds stay loose on
# purpose — a market holiday means no runs at all, and a false alarm on
# Thanksgiving trains you to ignore the real one.
#
# If this fires: check `kubectl -n claude get pods`, the messaging-gateway
# container logs, and `!status` over Signal. Manual takeover from this machine
# still works — the Robinhood MCP + memory live under ~ for zachd — but mind
# that the pod may have rotated the MCP refresh token since; re-auth with
# /mcp in an interactive claude session if the server refuses.

set -euo pipefail

REPO="${TRADING_JOURNAL_REPO:-git@github.com:zdiemer/trading-journal.git}"
CACHE="${TRADING_WATCHDOG_CACHE:-$HOME/.cache/trading-watchdog}"
MAX_AGE_HOURS="${TRADING_MAX_AGE_HOURS:-28}"
MONDAY_MAX_AGE_HOURS="${TRADING_MONDAY_MAX_AGE_HOURS:-76}"

if [ "$(date +%u)" = 1 ]; then
  MAX_AGE_HOURS="$MONDAY_MAX_AGE_HOURS"
fi

if [ ! -d "$CACHE/.git" ]; then
  rm -rf "$CACHE"
  git clone --quiet --depth 1 --single-branch "$REPO" "$CACHE"
fi
git -C "$CACHE" fetch --quiet --depth 1 origin main

last=$(git -C "$CACHE" log -1 --format=%ct origin/main)
now=$(date +%s)
age_hours=$(((now - last) / 3600))

if [ "$age_hours" -gt "$MAX_AGE_HOURS" ]; then
  echo "trading agent silent: last journal push was ${age_hours}h ago" \
    "(threshold ${MAX_AGE_HOURS}h) — the scheduled runs in claude-workspace" \
    "are not completing. Check the pod, gateway logs, and !status on Signal." >&2
  exit 1
fi

echo "trading journal fresh: last push ${age_hours}h ago (threshold ${MAX_AGE_HOURS}h)"
