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
# Requires: docker login ghcr.io  (with a PAT that has write:packages).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $0}' "${HERE}/values.yaml" | awk '{print $2}')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

command -v docker >/dev/null || { echo "docker required"; exit 1; }

echo "==> Building ${IMAGE}"
docker build -t "${IMAGE}" "${HERE}"

echo "==> Pushing ${IMAGE}"
docker push "${IMAGE}"

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
echo "    (First push only: set the GHCR package visibility to Public so the"
echo "     node can pull it without an imagePullSecret.)"
