#!/usr/bin/env bash
# Build the rachelfreeman image and push it to GHCR.
#
# ⚠️ This is break-glass now. The supported path is a push to main, which builds
# on GitHub Actions (.github/workflows/build.yml in zdiemer/rachelfreeman) and
# pushes ghcr.io/zdiemer/rachelfreeman:sha-<short>; roll onto it with deploy.sh.
# That keeps the write:packages credential out of the cluster entirely — GitHub
# cannot scope a classic PAT to one package — and is the only path available to
# the delegated workspace, which has no registry credential at all.
#
# Use this when Actions is down, or to build an uncommitted tree.
#
# The source is a separate repo (~/code/rachelfreeman); this script builds from
# there and tags with the version in values.yaml. Same shape as
# web/apartment-watch/build.sh: ghcr.io rather than a side-load, because the
# cluster is multi-node and a side-loaded image only exists on one of them.
#
# DO NOT build this inside the claude-workspace pod's shell. `next build` wants
# ~4Gi and that cgroup is shared with the messaging gateway — on 2026-08-15
# doing exactly that OOM-killed the gateway twice. buildctl below runs the build
# on the in-cluster buildkitd instead, which is the whole point.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${SRC:-${HOME}/code/rachelfreeman}"

REPO="$(awk '/^  repository:/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

[[ -n "$REPO" && -n "$TAG" ]] || { echo "could not read image repository/tag from values.yaml"; exit 1; }
[[ -d "$SRC" ]] || { echo "source repo not found at ${SRC} — set SRC=/path/to/rachelfreeman"; exit 1; }
[[ -f "${SRC}/Dockerfile" ]] || { echo "no Dockerfile in ${SRC}"; exit 1; }

# The chart's appVersion is supposed to track image.tag; catch the drift here
# rather than wondering later which commit is actually running.
CHART_APPVERSION="$(awk -F'"' '/^appVersion:/{print $2; exit}' "${HERE}/Chart.yaml")"
if [[ "$CHART_APPVERSION" != "$TAG" ]]; then
  echo "WARN: Chart.yaml appVersion (${CHART_APPVERSION}) != values.yaml image.tag (${TAG})" >&2
fi

if command -v buildctl >/dev/null; then
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — create the GHCR PAT file first"
    echo "(see dev/claude-workspace/README.md, Cluster powers)"; exit 1; }

  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${SRC}" \
    --local dockerfile="${SRC}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
elif command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${SRC}"
  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
else
  echo "buildctl or docker required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
echo "    (First push only: set the ghcr.io/zdiemer/rachelfreeman package"
echo "     visibility to Public so every node can pull it anonymously.)"
