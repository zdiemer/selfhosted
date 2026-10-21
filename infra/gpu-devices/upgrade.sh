#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
helm upgrade --install gpu-devices "$HERE" -n kube-system --wait --timeout 180s
