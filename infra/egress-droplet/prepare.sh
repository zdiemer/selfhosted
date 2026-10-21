#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export ANSIBLE_PIPELINING=true
uv run --with ansible-core==2.19.3 ansible-playbook -i "$HERE/inventory.yml" "$HERE/prepare.yml" "$@"
