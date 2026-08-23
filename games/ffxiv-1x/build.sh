#!/usr/bin/env bash
# Build the Garlemald Server image and push it to the in-cluster registry.
#
# The upstream repo at image.upstreamRef (values.yaml) IS the build context;
# the Dockerfile lives here. Bump upstreamRef and image.tag together, run
# this, then upgrade.sh. A cold Rust build of the workspace is ~8 minutes on
# the in-cluster buildkitd; the cargo cache mounts make later ones short.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk '/^  repository:/{print $2; exit}' "${HERE}/values.yaml")"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
REF="$(awk -F'"' '/^  upstreamRef:/{print $2; exit}' "${HERE}/values.yaml")"
UPSTREAM="https://github.com/swstegall/Garlemald-Server.git"
IMAGE="${REPO}:${TAG}"
[[ -n "$REF" ]] || { echo "image.upstreamRef missing in values.yaml"; exit 1; }

# The upstream repo is the build context, but buildkit's dockerfile frontend
# reads the Dockerfile from the CONTEXT when that context is a git URL, and
# upstream has none. So: shallow-fetch the pinned commit into a scratch dir,
# drop our Dockerfile in, and build from that.
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT
echo "==> Fetching ${UPSTREAM}#${REF}"
git -C "$CTX" init -q
git -C "$CTX" fetch -q --depth 1 "$UPSTREAM" "$REF"
git -C "$CTX" checkout -q FETCH_HEAD
cp "${HERE}/Dockerfile" "$CTX/Dockerfile"

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "$CTX"
  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first (selfhosted/infra/registry/README.md)"; exit 1; }
  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="$CTX" \
    --local dockerfile="$CTX" \
    --output "type=image,\"name=${IMAGE}\",push=${PUSH:-true}"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
