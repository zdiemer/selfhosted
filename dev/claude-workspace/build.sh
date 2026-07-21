#!/usr/bin/env bash
# Build the claude-workspace image and push it to GHCR. We don't run an
# in-cluster registry, so we ship via ghcr.io (public package) rather than
# side-loading into containerd — a side-loaded image with pullPolicy: Never
# gets reclaimed by kubelet image GC while the pod is down and can never be
# pulled back (see minecraft/claude-bridge/build.sh for the war story).
#
# Re-run whenever you edit the Dockerfile, then run upgrade.sh (the static
# tag + pullPolicy: Always means a pod restart picks up the new image).
#
# Requires: docker login ghcr.io  (with a PAT that has write:packages).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $2; exit}' "${HERE}/values.yaml")"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

command -v docker >/dev/null || { echo "docker required"; exit 1; }

echo "==> Building ${IMAGE}"
docker build -t "${IMAGE}" "${HERE}"

echo "==> Pushing ${IMAGE}"
docker push "${IMAGE}"

echo "==> Done. Run upgrade.sh (or delete the pod) to roll onto the new image."
echo "    (First push only: set the GHCR package visibility to Public so the"
echo "     nodes can pull it without an imagePullSecret.)"
