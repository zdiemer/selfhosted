#!/usr/bin/env bash
# Build and push the 86Box engine image.
#
# Separate from build.sh and build-rpcemu.sh for the same reason those are
# separate from each other: it moves when 86Box or its ROM set moves, which is
# nothing to do with when the UI changes. Same immutable-tag rule.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

REPO="$(awk '/^  box86Image:/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
TAG="$(awk -F'"' '/^  box86Tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"
[[ -n "$REPO" && -n "$TAG" ]] || { echo "could not read vm.box86Image/box86Tag from values.yaml"; exit 1; }

if [[ "${VMLAB_ALLOW_TAG_OVERWRITE:-0}" != "1" ]]; then
  if docker manifest inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "FAIL: ${IMAGE} already exists. Bump vm.box86Tag in values.yaml."
    exit 1
  fi
fi

if command -v docker >/dev/null; then
  echo "==> Building + pushing ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}/x86box"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  echo "==> Building + pushing ${IMAGE} (buildctl -> ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}/x86box" \
    --local dockerfile="${HERE}/x86box" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done."
