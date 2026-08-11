#!/usr/bin/env bash
# Measure how long a service is actually unreachable across an upgrade or a
# node drain.
#
# WHY THIS EXISTS
# Every chart in this repo has an opinion about its rollout strategy, and until
# now none of them had a number. "Recreate takes a brief gap" and "this rolls
# with no downtime" were both assertions, and the second one was wrong at least
# twice — whatnowgg's surge deadlocked on an iSCSI volume, and minecraft's
# `strategy:` key was inert — without anything noticing, because nobody was
# holding a stopwatch. This is the stopwatch.
#
# Run it before a change to get a baseline, and after to show the change did
# what it claimed. A phase of the availability work that cannot produce a
# before/after pair here has not been demonstrated, only argued.
#
# WHAT COUNTS AS DOWN
# A response — any response — means the service answered, so 3xx and 4xx are UP.
# An unauthenticated request to an Authelia-gated host correctly returns 302,
# and a 404 means routing works and the app is serving. DOWN is: no HTTP
# response at all (connection refused, reset, timeout) or a 5xx, because with
# no ready endpoints behind it Traefik synthesises a 503. That is exactly the
# window a user experiences as "the site is down".
#
# USAGE
#   # watch for 60s while you do something else
#   scripts/measure-gap.sh https://games.zachd.duckdns.org
#
#   # measure an upgrade, end to end
#   scripts/measure-gap.sh https://games.zachd.duckdns.org -- games/gamedex/upgrade.sh
#
#   # measure a drain against several hosts at once
#   scripts/measure-gap.sh https://status.diemer.codes https://kelsey.zachd.duckdns.org \
#       -- kubectl drain zachd-ubuntu-laptop-6 --ignore-daemonsets --delete-emptydir-data
#
# Exit status is the command's own, so this can wrap a step in a script without
# swallowing its failure.

set -uo pipefail

INTERVAL_MS=200
DURATION=60
SETTLE=10
URLS=()
CMD=()

usage() {
    sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval-ms) INTERVAL_MS="$2"; shift 2 ;;
        --duration)    DURATION="$2"; shift 2 ;;
        --settle)      SETTLE="$2"; shift 2 ;;
        -h|--help)     usage ;;
        --)            shift; CMD=("$@"); break ;;
        -*)            echo "Unknown option: $1" >&2; exit 2 ;;
        *)             URLS+=("$1"); shift ;;
    esac
done

if [[ ${#URLS[@]} -eq 0 ]]; then
    echo "Error: at least one URL is required. See --help." >&2
    exit 2
fi

command -v curl >/dev/null || { echo "curl required" >&2; exit 1; }

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# One poller per URL rather than one loop over all of them: a slow host must not
# push the others off their cadence, which is the whole point of sampling at a
# fixed interval.
poll() {
    local url=$1 out=$2
    local interval
    interval=$(awk -v ms="$INTERVAL_MS" 'BEGIN { printf "%.3f", ms/1000 }')
    while :; do
        local now code
        now=$(date +%s%3N)
        # --max-time bounds a hang so the sample rate degrades gracefully
        # instead of the poller silently stalling and under-reporting the gap.
        code=$(curl -s -o /dev/null -w '%{http_code}' \
                    --max-time 3 --connect-timeout 2 "$url" 2>/dev/null) || code="000"
        printf '%s %s\n' "$now" "$code" >>"$out"
        sleep "$interval"
    done
}

PIDS=()
for i in "${!URLS[@]}"; do
    poll "${URLS[$i]}" "$WORKDIR/$i.samples" &
    PIDS+=("$!")
done

stop_pollers() {
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
    done
    PIDS=()
}

RC=0
if [[ ${#CMD[@]} -gt 0 ]]; then
    echo "==> Measuring ${#URLS[@]} host(s) while running: ${CMD[*]}"
    echo
    "${CMD[@]}"
    RC=$?
    echo
    echo "==> Command exited ${RC}; settling for ${SETTLE}s"
    sleep "$SETTLE"
else
    echo "==> Measuring ${#URLS[@]} host(s) for ${DURATION}s"
    sleep "$DURATION"
fi

stop_pollers

echo
echo "================ availability ================"
WORST=0
for i in "${!URLS[@]}"; do
    echo
    echo "${URLS[$i]}"
    # Analysis is one awk pass: walk the samples in order, and treat a run of
    # consecutive DOWN samples as one outage. Its duration is measured from the
    # last good sample to the first good sample after it, so it includes the
    # sampling blind spot on both sides rather than flattering the result.
    awk '
        function flush(  dur) {
            if (down_since == 0) return
            dur = (last_up_after > 0 ? last_up_after : $1) - down_since
            outages[++n] = dur
            total += dur
            if (dur > worst) worst = dur
            down_since = 0
        }
        {
            ts = $1; code = $2
            samples++
            is_down = (code == "000" || code+0 >= 500)
            if (is_down) {
                fails++
                if (down_since == 0) down_since = (prev_ts ? prev_ts : ts)
                last_up_after = 0
            } else {
                if (down_since != 0) { last_up_after = ts; flush() }
                prev_ts = ts
            }
        }
        END {
            if (down_since != 0) { last_up_after = 0; flush() }
            if (samples == 0) { print "  no samples"; exit }
            printf "  samples=%d  failed=%d  (%.1f%%)\n", samples, fails, 100*fails/samples
            if (n == 0) { print "  NO GAP - every sample answered"; print "WORST 0"; exit }
            printf "  outages=%d  longest=%.1fs  total=%.1fs\n", n, worst/1000, total/1000
            for (i = 1; i <= n; i++) printf "    outage %d: %.1fs\n", i, outages[i]/1000
            printf "WORST %d\n", worst
        }
    ' "$WORKDIR/$i.samples" | tee "$WORKDIR/$i.report" | grep -v '^WORST '
    w=$(awk '/^WORST /{print $2}' "$WORKDIR/$i.report")
    [[ -n "$w" && "$w" -gt "$WORST" ]] && WORST=$w
done

echo
printf '==> Worst gap across all hosts: %.1fs\n' "$(awk -v w="$WORST" 'BEGIN{print w/1000}')"
exit "$RC"
