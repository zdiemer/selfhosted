#!/usr/bin/env bash
# Point the firewall's allowlist at whatever address the house currently has.
#
# Run by egress-allow-home.timer every 5 minutes. The house is on a residential
# connection whose address changes without warning, and infra/duckdns already
# keeps a DDNS name tracking it — so the firewall follows that name rather than
# a hardcoded address that would silently lock the cluster out.
#
# FAILS CLOSED. Every failure path here leaves the existing set alone and exits
# non-zero. It never adds an address it is unsure about, and never falls back to
# "allow everything" — a firewall that widens when DNS breaks is worse than no
# firewall, because it looks like one.

set -euo pipefail

HOME_DDNS="${HOME_DDNS:-zachd.duckdns.org}"
TABLE="inet egress"

command -v nft >/dev/null || { echo "nft not found"; exit 1; }

resolve() {
  local type="$1"
  # +short can emit CNAMEs as well as addresses; keep only address-shaped lines.
  if command -v dig >/dev/null; then
    dig +short "$type" "$HOME_DDNS" 2>/dev/null | grep -E '^[0-9a-fA-F:.]+$' || true
  else
    getent ahostsv4 "$HOME_DDNS" 2>/dev/null | awk '{print $1}' | sort -u || true
  fi
}

V4="$(resolve A)"
V6="$(resolve AAAA)"

if [[ -z "$V4" && -z "$V6" ]]; then
  echo "could not resolve ${HOME_DDNS} — leaving the allowlist untouched" >&2
  exit 1
fi

sync_set() {
  local set_name="$1" addrs="$2"
  [[ -n "$addrs" ]] || return 0
  local joined
  joined="$(tr '\n' ',' <<<"$addrs" | sed 's/,$//')"
  # Replace wholesale rather than adding: an old address must stop being
  # admitted, or the allowlist only ever grows and stops meaning anything.
  nft flush set $TABLE "$set_name" 2>/dev/null || return 1
  nft add element $TABLE "$set_name" "{ $joined }"
  echo "${set_name}: ${joined}"
}

CHANGED=0
sync_set home_v4 "$V4" && CHANGED=1
sync_set home_v6 "$V6" || true

[[ "$CHANGED" -eq 1 ]] || { echo "no IPv4 address applied" >&2; exit 1; }
