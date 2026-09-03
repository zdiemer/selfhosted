#!/usr/bin/env bash
# Build and push the hatch image. Run this before upgrade.sh whenever anything
# under app/ changes, and bump image.tag first — imagePullPolicy is
# IfNotPresent, so reusing a tag will not re-pull on the other nine nodes.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

REPO="$(awk '/^  repository:/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"
[[ -n "$REPO" && -n "$TAG" ]] || { echo "could not read image repository/tag from values.yaml"; exit 1; }

CHART_APPVERSION="$(awk -F'"' '/^appVersion:/{print $2; exit}' "${HERE}/Chart.yaml")"
if [[ "$CHART_APPVERSION" != "$TAG" ]]; then
  echo "WARN: Chart.yaml appVersion (${CHART_APPVERSION}) != values.yaml image.tag (${TAG})" >&2
fi

# Tags must be immutable here, unlike most charts. The Deployment uses
# imagePullPolicy: IfNotPresent so that hatch can still start on a node that
# cannot reach GHCR — which is only safe if a cached layer is guaranteed to be
# the same image the tag names.
if [[ "${HATCH_ALLOW_TAG_OVERWRITE:-0}" != "1" ]]; then
  if docker manifest inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "FAIL: ${IMAGE} already exists in the registry."
    echo "      Bump image.tag in values.yaml and appVersion in Chart.yaml."
    echo "      (HATCH_ALLOW_TAG_OVERWRITE=1 to override — see the note above.)"
    exit 1
  fi
fi

if command -v docker >/dev/null; then
  echo "==> Building + pushing ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — buildctl needs registry credentials"; exit 1; }
  echo "==> Building + pushing ${IMAGE} (buildctl -> ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the Deployment onto it."
