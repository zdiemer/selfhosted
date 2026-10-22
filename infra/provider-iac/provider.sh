#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Avoid shell tracing of the session export.
set +x
session_line="$("$HERE/../../scripts/op-session.sh" ensure)"
[[ -z "$session_line" ]] || eval "$session_line"
unset session_line
case "${1:-}" in
  discover) shift; exec python3 "$HERE/discover.py" "$@" ;;
  *) exec python3 "$HERE/run.py" "$@" ;;
esac
