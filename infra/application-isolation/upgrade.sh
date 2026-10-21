#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
helm upgrade --install application-isolation "$HERE" -n infra --wait --timeout 120s
