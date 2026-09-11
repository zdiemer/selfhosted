#!/usr/bin/env bash
# Build the cloud-game image and push it to the in-cluster registry (infra/registry).
#
# Upstream ships no container image, only a multi-stage Dockerfile, so we build
# THEIR repo at a pinned commit (image.upstreamRef in values.yaml) with the
# `cloud-game` target, which bundles coordinator + worker + llvmpipe GL.
# Nothing here is ours: there is no local Dockerfile, the git URL is the build
# context. Bump upstreamRef AND image.tag together, re-run, then upgrade.sh.
#
# The build compiles GStreamer from source: expect 10-20 minutes on the
# in-cluster buildkitd the first time. Later builds hit its cache.
#
# Requires: docker login registry.zachd.duckdns.org on a laptop, or —
# inside the claude-workspace pod, where there is no docker — buildctl + the
# in-cluster buildkitd (infra/buildkit) + the registry credential in ~/.docker/config.json
# (see selfhosted/infra/registry/README.md).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $0}' "${HERE}/values.yaml" | awk '{print $2}')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"
UPSTREAM="https://github.com/giongto35/cloud-game.git"
REF="$(awk -F'"' '/^  upstreamRef:/{print $2; exit}' "${HERE}/values.yaml")"
[[ -n "$REF" ]] || { echo "image.upstreamRef missing in values.yaml"; exit 1; }

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} from ${UPSTREAM}#${REF} (docker)"
  docker build --target cloud-game --build-arg "VERSION=${REF}" \
    -t "${IMAGE}" "${UPSTREAM}#${REF}"

  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first (selfhosted/infra/registry/README.md)"
    echo "(see selfhosted/infra/registry/README.md)"; exit 1; }

  echo "==> Building + pushing ${IMAGE} from ${UPSTREAM}#${REF} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --opt "context=${UPSTREAM}#${REF}" \
    --opt target=cloud-game \
    --opt "build-arg:VERSION=${REF}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
