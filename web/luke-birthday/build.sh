#!/usr/bin/env bash
# Build the luke.diemer.codes image and push it to the in-cluster registry (infra/registry). The cluster is
# multi-node, so we ship via the in-cluster registry (infra/registry) rather
# than side-loading into each node's containerd. Re-run after
# editing site/, the Dockerfile or nginx.conf, then run upgrade.sh.
#
# Requires: docker login registry.zachd.duckdns.org on a laptop, or —
# inside the claude-workspace pod, where there is no docker — buildctl + the
# in-cluster buildkitd (infra/buildkit) + the registry credential in ~/.docker/config.json
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
  # Workspace-pod path: remote build on the in-cluster buildkitd, which pushes
  # straight to the registry. Auth is forwarded per-session from ~/.docker/config.json.
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first (selfhosted/infra/registry/README.md)"
    echo "(see selfhosted/infra/registry/README.md)"; exit 1; }

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
