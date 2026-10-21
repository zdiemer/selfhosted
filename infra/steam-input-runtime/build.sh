#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
buildctl build --frontend dockerfile.v0 \
  --local context="$HERE/plugin" --local dockerfile="$HERE/plugin" \
  --output type=image,name=registry.zachd.duckdns.org/zdiemer/steam-input-runtime:input-v1,push=true
# Copy the resulting digest into values.yaml before deployment.
