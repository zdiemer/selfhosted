#!/usr/bin/env bash
# Build the happy-server image from the slopus/happy monorepo (upstream
# publishes no server image) and push it to GHCR. Same shipping rationale as
# every other chart: public GHCR package, never side-loaded — see
# minecraft/claude-bridge/build.sh for the war story.
#
# The monorepo's Dockerfile.server builds from the repo ROOT (pnpm workspace),
# so we shallow-clone at a pinned ref and hand buildkit the whole checkout.
# Bump HAPPY_REF and image.tag in values.yaml together, deliberately — this
# is an internet-facing surface.
#
# Requires: git, plus docker login ghcr.io (PAT with write:packages) on a
# laptop, or — inside the workspace pod — buildctl + the in-cluster buildkitd
# (infra/buildkit) + a GHCR PAT in ~/.docker/config.json.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $2; exit}' "${HERE}/values.yaml")"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

# Monorepo tag (or branch/sha) to build. cli-1.1.10 = June 2026 release.
HAPPY_REF="${HAPPY_REF:-cli-1.1.10}"

SRC="$(mktemp -d)"
trap 'rm -rf "${SRC}"' EXIT

echo "==> Cloning slopus/happy @ ${HAPPY_REF}"
git clone --depth 1 --branch "${HAPPY_REF}" \
  https://github.com/slopus/happy.git "${SRC}"

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -f "${SRC}/Dockerfile.server" -t "${IMAGE}" "${SRC}"

  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  # Workspace-pod path: remote build on the in-cluster buildkitd, which pushes
  # straight to GHCR. Auth is forwarded per-session from ~/.docker/config.json.
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — create the GHCR PAT file first"
    echo "(see dev/claude-workspace/README.md, Cluster powers)"; exit 1; }

  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${SRC}" \
    --local dockerfile="${SRC}" \
    --opt filename=Dockerfile.server \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh (or delete the pod) to roll onto the new image."
echo "    (First push only: set the GHCR package visibility to Public so the"
echo "     nodes can pull it without an imagePullSecret.)"
