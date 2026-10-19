#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
line="$("$HERE/../../scripts/op-session.sh" ensure)"
[[ -z "$line" ]] || eval "$line"
exec python3 "$HERE/configure-notifications.py" "$@"
