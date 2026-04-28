#!/usr/bin/env bash
# Build the claude-bridge container image and side-load it into k3s
# containerd. We don't run a registry on this cluster, so this is the
# simplest way to ship a locally-built image to k3s.
#
# Re-run whenever you edit Dockerfile or anything under src/.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="claude-bridge:${TAG}"

command -v docker >/dev/null || { echo "docker required"; exit 1; }
command -v k3s    >/dev/null || { echo "k3s required (this script imports into k3s containerd)"; exit 1; }

echo "==> Building ${IMAGE}"
docker build -t "${IMAGE}" "${HERE}"

echo "==> Importing ${IMAGE} into k3s containerd"
# `docker save | k3s ctr images import` is the standard side-load path on
# single-node k3s. Needs sudo because k3s ctr talks to the root-owned socket.
docker save "${IMAGE}" | sudo k3s ctr images import -

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
