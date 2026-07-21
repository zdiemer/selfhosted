#!/usr/bin/env bash
# Build the claude-bridge image and push it to GHCR. We don't run an
# in-cluster registry, so we ship via ghcr.io (public package) rather than
# side-loading into containerd — a side-loaded image with pullPolicy: Never
# gets reclaimed by kubelet image GC while the pod is down and can never be
# pulled back, which silently killed the bridge for weeks.
#
# Re-run whenever you edit Dockerfile or anything under src/, then run
# upgrade.sh.
#
# Requires: docker login ghcr.io (PAT with write:packages) on a laptop, or —
# inside the claude-workspace pod, where there is no docker — buildctl + the
# in-cluster buildkitd (infra/buildkit) + a GHCR PAT in ~/.docker/config.json
# (see dev/claude-workspace/README.md, "Cluster powers").

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $0}' "${HERE}/values.yaml" | awk '{print $2}')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"

  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  # Workspace-pod path: remote build on the in-cluster buildkitd, which pushes
  # straight to GHCR. Auth is forwarded per-session from ~/.docker/config.json.
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — create the GHCR PAT file first"
    echo "(see dev/claude-workspace/README.md, Cluster powers)"; exit 1; }

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
echo "    (First push only: set the GHCR package visibility to Public so the"
echo "     node can pull it without an imagePullSecret.)"
