#!/usr/bin/env bash
# Build the gamedex image and side-load it into k3s containerd. We don't run a
# registry on this cluster, so `docker save | k3s ctr images import` is the
# simplest way to ship a locally-built image. Re-run after editing anything
# under src/, static/, or the Dockerfile.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="gamedex:${TAG}"

command -v docker >/dev/null || { echo "docker required"; exit 1; }
command -v k3s    >/dev/null || { echo "k3s required (this script imports into k3s containerd)"; exit 1; }

echo "==> Building ${IMAGE}"
docker build -t "${IMAGE}" "${HERE}"

echo "==> Importing ${IMAGE} into k3s containerd"
# Needs sudo because k3s ctr talks to the root-owned containerd socket.
docker save "${IMAGE}" | sudo k3s ctr images import -

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
