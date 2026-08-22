#!/usr/bin/env bash
# Build the COMP_hack server image and push it to the in-cluster registry.
#
# The Dockerfile fetches the 4.12.2 source tarball from Launchpad itself
# (sha256-pinned), so the build context is just this directory. Bump image.tag
# in values.yaml for any Dockerfile change, re-run, then upgrade.sh.
#
# Expect ~10-15 minutes on the in-cluster buildkitd the first time (it is a
# large C++14 tree and the bundled deps — asio, civetweb, squirrel, sqlite —
# build from source). Later builds hit the layer cache.
#
# Requires: docker login registry.zachd.duckdns.org on a laptop, or — inside the
# claude-workspace pod, where there is no docker — buildctl + the in-cluster
# buildkitd (infra/buildkit) + the registry credential in ~/.docker/config.json
# (see selfhosted/infra/registry/README.md).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk '/^  repository:/{print $2; exit}' "${HERE}/values.yaml")"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"
  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first (selfhosted/infra/registry/README.md)"; exit 1; }
  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
