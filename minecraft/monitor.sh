#!/usr/bin/env bash
# Lightweight TPS + memory poller for the Minecraft pod.
#
#   ./monitor.sh                       # poll every 60s until Ctrl-C
#   INTERVAL=30 ./monitor.sh           # override interval
#   MEM_LIMIT_MI=10240 ./monitor.sh    # container memory limit (for % calc)
#
# One sample = trigger `spark tps`, tail the async reply from server logs,
# read container RSS via `kubectl top`. Flags TPS < 19.5 or mem >= 85%.
set -euo pipefail

NS="${NAMESPACE:-minecraft}"
REL="${RELEASE:-mc}"
INTERVAL="${INTERVAL:-60}"
TPS_WARN="${TPS_WARN:-19.5}"
MEM_WARN_PCT="${MEM_WARN_PCT:-85}"
MEM_LIMIT_MI="${MEM_LIMIT_MI:-10240}"

DEPLOY="deploy/${REL}-minecraft"
CTR="mc-minecraft"

sample() {
  kubectl -n "$NS" exec "$DEPLOY" -c "$CTR" -- rcon-cli "spark tps" >/dev/null 2>&1 || return 1

  local out tps_line tick_line
  for _ in 1 2 3 4 5 6 7 8; do
    sleep 1
    out=$(kubectl -n "$NS" logs "$DEPLOY" -c "$CTR" --since=15s 2>/dev/null \
            | grep -F '[⚡]' || true)
    tps_line=$(printf '%s\n' "$out"  | grep -A1 'TPS from last'   | tail -1 || true)
    tick_line=$(printf '%s\n' "$out" | grep -A1 'Tick durations' | tail -1 || true)
    [[ -n "$tps_line" && -n "$tick_line" ]] && break
  done

  local tps_1m tick_med_1m
  tps_1m=$(printf '%s' "$tps_line"       | tr -d '*' | grep -oE '[0-9]+\.[0-9]+' | sed -n '3p')
  tick_med_1m=$(printf '%s' "$tick_line" | grep -oE '[0-9]+\.[0-9]+' | sed -n '6p')

  local mem_mi
  mem_mi=$(kubectl top pod -n "$NS" --no-headers 2>/dev/null \
             | awk -v d="${REL}-minecraft" '$1 ~ d {gsub("Mi","",$3); print $3; exit}')

  local mem_pct=0
  if [[ -n "${mem_mi:-}" && "$MEM_LIMIT_MI" -gt 0 ]]; then
    mem_pct=$(awk -v m="$mem_mi" -v l="$MEM_LIMIT_MI" 'BEGIN{printf "%.0f", m*100/l}')
  fi

  local flag=""
  if [[ -n "${tps_1m:-}" ]] && awk -v t="$tps_1m" -v w="$TPS_WARN" 'BEGIN{exit !(t<w)}'; then
    flag="$flag TPS_LOW"
  fi
  (( mem_pct >= MEM_WARN_PCT )) && flag="$flag MEM_HIGH"

  printf '%s  TPS(1m)=%-6s tick_med=%-5sms  mem=%sMi (%d%%)%s\n' \
    "$(date '+%F %T')" \
    "${tps_1m:-?}" \
    "${tick_med_1m:-?}" \
    "${mem_mi:-?}" \
    "$mem_pct" \
    "${flag:+  WARN:$flag}"
}

echo "# poll every ${INTERVAL}s  |  warn: TPS<${TPS_WARN}, mem>=${MEM_WARN_PCT}% of ${MEM_LIMIT_MI}Mi  |  Ctrl-C to stop"
while true; do
  sample || echo "$(date '+%F %T')  sample failed"
  sleep "$INTERVAL"
done
