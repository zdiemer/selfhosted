#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APPVERSION="$(awk -F'"' '/^appVersion:/{print $2; exit}' "${HERE}/Chart.yaml")"
REPOSITORY="$(python3 -c "import yaml; print(yaml.safe_load(open('${HERE}/values.yaml'))['image']['repository'])")"
TAG="$(python3 -c "import yaml; print(yaml.safe_load(open('${HERE}/values.yaml'))['image']['tag'])")"

[[ "$APPVERSION" == "$TAG" ]] || { echo "image.tag=${TAG} != appVersion=${APPVERSION}"; exit 1; }
IMAGE="${REPOSITORY}:${TAG}"

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker build -t "$IMAGE" "$HERE"
  docker push "$IMAGE"
elif command -v buildctl >/dev/null; then
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="$HERE" \
    --local dockerfile="$HERE" \
    --output "type=image,name=${IMAGE},push=true"
else
  echo "docker or buildctl required" >&2
  exit 1
fi
