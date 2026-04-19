#!/usr/bin/env bash
# Open an RCON session on the running server.
#
#   ./rcon.sh                 # interactive shell
#   ./rcon.sh list            # one-shot command
#   ./rcon.sh say hello team  # multi-word command
set -euo pipefail
exec kubectl -n "${NAMESPACE:-minecraft}" exec -it \
  "deploy/${RELEASE:-mc}-minecraft" -c mc-minecraft -- rcon-cli "$@"
