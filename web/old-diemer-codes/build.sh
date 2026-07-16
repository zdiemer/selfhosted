#!/usr/bin/env bash
# Build the old.diemer.codes image and push it to GHCR. The cluster is multi-node
# with no in-cluster registry, so we ship via ghcr.io (public package) rather than
# side-loading into each node's containerd. Re-run after moving the site/ submodule
# pin or editing the Dockerfile / nginx.conf / overlay, then run upgrade.sh.
#
# Requires: docker login ghcr.io  (with a PAT that has write:packages).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk -F'"' '/^  repository:/{print $0}' "${HERE}/values.yaml" | awk '{print $2}')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

command -v docker >/dev/null || { echo "docker required"; exit 1; }

# The source is a submodule, so a `git clone` without --recurse-submodules leaves
# site/ empty and docker fails with an opaque "COPY site/package.json: not found".
if [[ ! -f "${HERE}/site/package.json" ]]; then
  echo "site/ is empty — the app source is a submodule. Run:"
  echo "  git submodule update --init web/old-diemer-codes/site"
  exit 1
fi

echo "==> Building ${IMAGE}"
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

echo "==> Done. Run upgrade.sh to roll the deployment onto the new image."
echo "    (First push only: set the GHCR package visibility to Public so nodes"
echo "     can pull it without an imagePullSecret.)"
