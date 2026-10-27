#!/usr/bin/env bash
# Keep the existing maintenance entry point; the guard covers every art tier.
set -euo pipefail
exec timeout --signal=TERM --kill-after=10s 120s python3 "$(dirname "$0")/art-evict.py"
