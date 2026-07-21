#!/usr/bin/env bash
# Build the old.diemer.codes image and push it to GHCR. The cluster is multi-node
# with no in-cluster registry, so we ship via ghcr.io (public package) rather than
# side-loading into each node's containerd. Re-run after moving the site/ submodule
# pin or editing the Dockerfile / nginx.conf / overlay, then run upgrade.sh.
#
# Requires: docker login ghcr.io (PAT with write:packages) on a laptop, or —
# inside the claude-workspace pod, where there is no docker — buildctl + the
# in-cluster buildkitd (infra/buildkit) + a GHCR PAT in ~/.docker/config.json
# (see dev/claude-workspace/README.md, "Cluster powers").

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $0}' "${HERE}/values.yaml" | awk '{print $2}')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

# The source is a submodule, so a `git clone` without --recurse-submodules leaves
# site/ empty and docker fails with an opaque "COPY site/package.json: not found".
if [[ ! -f "${HERE}/site/package.json" ]]; then
  echo "site/ is empty — the app source is a submodule. Run:"
  echo "  git submodule update --init web/old-diemer-codes/site"
  exit 1
fi

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"

  # The Dockerfile already asserts this, but assert it again against the FINAL
  # image, because the failure it guards is completely silent at runtime: without
  # site.css the site returns 200, renders unstyled, and logs nothing anywhere.
  # site.css is produced by lessc via `npm run build-css`, is gitignored upstream,
  # and `npm run build` does not generate it. See the Dockerfile.
  echo "==> Verifying the built image is actually styled"
  docker run --rm --entrypoint sh "${IMAGE}" -c '
    set -eu
    test -s /usr/share/nginx/html/site.css
    grep -q "about-me-body" /usr/share/nginx/html/site.css
    grep -q "site\.css"     /usr/share/nginx/html/index.html
  ' || { echo "!! site.css missing or empty — DO NOT PUSH. See the build-css note in the Dockerfile."; exit 1; }

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
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"

  # The docker-run styled-check above can't run against a remote build (the
  # image goes straight to GHCR, never lands here). The Dockerfile's own
  # assert still gates the build, but the belt-and-braces re-check is skipped.
  echo "NOTE: skipped the post-build site.css verification (remote build);"
  echo "      the Dockerfile's build-time assert is the only styling gate."
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
echo "    (First push only: set the GHCR package visibility to Public so nodes"
echo "     can pull it without an imagePullSecret.)"
