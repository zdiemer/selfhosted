#!/usr/bin/env bash
# Keep the existing maintenance entry point; the guard covers every art tier.
set -euo pipefail
exec python3 "$(dirname "$0")/art-evict.py"
