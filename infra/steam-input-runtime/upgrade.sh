#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/apply-node-profiles.py"
helm upgrade --install steam-input-runtime "$HERE" -n kube-system --wait --timeout 180s
