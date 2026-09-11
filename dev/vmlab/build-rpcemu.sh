#!/usr/bin/env bash
# Build and push the RPCEmu engine image — the RISC OS half of the lab.
#
# Separate from build.sh because it changes on a completely different schedule:
# the UI image is rebuilt whenever app/ changes, this one only when RPCEmu or
# the RISC OS release moves. Same immutable-tag rule, for the same reason.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

REPO="$(awk '/^  rpcemuImage:/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
TAG="$(awk -F'"' '/^  rpcemuTag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"
[[ -n "$REPO" && -n "$TAG" ]] || { echo "could not read vm.rpcemuImage/rpcemuTag from values.yaml"; exit 1; }

if [[ "${VMLAB_ALLOW_TAG_OVERWRITE:-0}" != "1" ]]; then
  if docker manifest inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "FAIL: ${IMAGE} already exists. Bump vm.rpcemuTag in values.yaml."
    exit 1
  fi
fi

if command -v docker >/dev/null; then
  echo "==> Building + pushing ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}/rpcemu"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  echo "==> Building + pushing ${IMAGE} (buildctl -> ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}/rpcemu" \
    --local dockerfile="${HERE}/rpcemu" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done."
