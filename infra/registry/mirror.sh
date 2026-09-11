#!/usr/bin/env bash
# Copy an image from another registry into this one, same path and tag:
#
#     ./mirror.sh ghcr.io/zdiemer/money:v83            → registry.zachd.duckdns.org/zdiemer/money:v83
#     ./mirror.sh ghcr.io/zdiemer/money:v83 ghcr.io/zdiemer/sms-relay:v1 ...
#
# For the one-time move off GHCR, and for any image that is built elsewhere
# (GitHub Actions) but should be pullable when GitHub is not. There is no
# skopeo/crane in the workspace image, so this is a buildkit build of a
# Dockerfile that is nothing but `FROM <source>`: every layer is re-used by
# digest and pushed as-is. The image *config* is re-serialized, so the
# resulting manifest digest differs from the source's — a chart pinned by
# digest needs its pin updated after mirroring, a chart pinned by tag does not.
#
# Requires buildctl + BUILDKIT_HOST (the workspace pod) or docker, and an auth
# entry for both registries in ~/.docker/config.json (README).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${REGISTRY_HOST:-$(awk '/^  host:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "${HERE}/values.yaml")}"

[[ $# -ge 1 ]] || { echo "usage: $0 <source-image:tag> [...]"; exit 1; }

for SRC in "$@"; do
  # Strip the source registry host; keep the namespace/name:tag.
  PATH_TAG="${SRC#*/}"
  DST="${HOST}/${PATH_TAG}"
  echo "==> ${SRC} → ${DST}"
  ctx="$(mktemp -d)"
  printf 'FROM %s\n' "$SRC" >"${ctx}/Dockerfile"
  if command -v buildctl >/dev/null; then
    buildctl build --frontend dockerfile.v0 \
      --local context="$ctx" --local dockerfile="$ctx" \
      --output "type=image,\"name=${DST}\",push=true" 2>&1 | grep -E 'ERROR|error|pushing|DONE' | tail -3
  elif command -v docker >/dev/null; then
    docker pull "$SRC" >/dev/null && docker tag "$SRC" "$DST" && docker push "$DST" >/dev/null
  else
    echo "docker or buildctl required"; exit 1
  fi
  rm -rf "$ctx"
  echo "    done"
done
