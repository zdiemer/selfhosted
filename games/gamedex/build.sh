#!/usr/bin/env bash
# Build the gamedex image and push it to GHCR. The cluster is multi-node with no
# in-cluster registry, so we ship via ghcr.io (public package) rather than
# side-loading into each node's containerd. Re-run after editing anything under
# src/, static/, or the Dockerfile, then run upgrade.sh.
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
echo "    (First push only: set the GHCR package visibility to Public so nodes"
echo "     can pull it without an imagePullSecret.)"
