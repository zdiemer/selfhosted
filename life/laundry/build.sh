#!/usr/bin/env bash
# Build the laundry image and push it to the in-cluster registry.
#
# Pushes to the in-cluster registry (infra/registry); every node pulls it
# through the registries.yaml mirror, so nothing is side-loaded into containerd.
#
# Re-run after editing anything under src/, then run upgrade.sh. Nothing here
# touches the ESP32 — the firmware is flashed with `pio run -t upload` from
# firmware/, and is deliberately not part of any cluster rollout.
#
# Requires: buildctl + the in-cluster buildkitd (infra/buildkit) and registry
# credentials in ~/.docker/config.json (op://homelab/infra-registry), or docker logged in to
# registry.zachd.duckdns.org.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk '/^  repository:/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

[[ -n "$REPO" && -n "$TAG" ]] || { echo "could not read image repository/tag from values.yaml"; exit 1; }

# The chart's appVersion is supposed to track image.tag; catch the drift here
# rather than wondering later which commit is actually running.
CHART_APPVERSION="$(awk -F'"' '/^appVersion:/{print $2; exit}' "${HERE}/Chart.yaml")"
if [[ "$CHART_APPVERSION" != "$TAG" ]]; then
  echo "WARN: Chart.yaml appVersion (${CHART_APPVERSION}) != values.yaml image.tag (${TAG})" >&2
fi

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"
  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first"
    echo "(see infra/registry/README.md)"; exit 1; }

  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the Deployment onto it."
