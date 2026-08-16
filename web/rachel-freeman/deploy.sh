#!/usr/bin/env bash
# Roll rachel-freeman onto a new image tag. No 1Password, no vault, no secrets.
#
# This is the entry point the delegated workspace uses (dev/claude-workspace,
# PROFILE=rachel). upgrade.sh is the one for a full deploy from a seat that has
# vault access; the difference is deliberate:
#
#   upgrade.sh  resolves values.local.yaml from 1Password and applies the whole
#               chart. Needs an op session. Use it when a secret changed, when
#               chart structure changed, or on a first install.
#   deploy.sh   applies the chart with --reuse-values, which carries the
#               PREVIOUS release's user-supplied values forward untouched. The
#               rendered Secrets come out identical without a single op://
#               lookup, so an instance with no vault credential can still ship
#               a code change.
#
# The tradeoff to know about: --reuse-values means a change to values.yaml's
# defaults will NOT take effect here if the same key was ever set in
# values.local.yaml. Structural chart changes are for upgrade.sh.
#
# Usage:
#   ./deploy.sh                 # roll onto image.tag from values.yaml
#   ./deploy.sh sha-1a2b3c4     # roll onto an explicit tag
#
# The tag comes from the GitHub Actions build (.github/workflows/build.yml in
# zdiemer/rachelfreeman) — a push to main produces ghcr.io/zdiemer/rachelfreeman
# :sha-<short>. Building here is not an option: `next build` wants ~4Gi and has
# OOM-killed a workspace pod's messaging gateway twice.

set -euo pipefail

RELEASE="${RELEASE:-rachel-freeman}"
NAMESPACE="${NAMESPACE:-rachel}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

REPO="$(awk '/^  repository:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$VALUES")"
TAG="${1:-$(awk '/^  tag:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$VALUES")}"
[[ -n "$TAG" ]] || { echo "no image tag: pass one, or set image.tag in values.yaml" >&2; exit 1; }

# There must already be a release to reuse values from. On a fresh namespace
# this is the wrong script — say so rather than deploying a chart with blank
# secrets that only fails once Payload tries to sign a cookie.
if ! helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "ERROR: no ${RELEASE} release in ${NAMESPACE} to reuse values from." >&2
  echo "       First install is upgrade.sh, from a seat with 1Password access." >&2
  exit 1
fi

# Refuse a tag that isn't in the registry. `--atomic` would roll back a failed
# deploy, but only after the rollout timeout — several minutes of the site
# serving from a half-replaced ReplicaSet for what is almost always a typo or a
# build that hasn't finished yet.
echo "==> Verifying ${REPO}:${TAG} exists"
if [[ "$REPO" == ghcr.io/* ]] && command -v curl >/dev/null && command -v python3 >/dev/null; then
  IMG_PATH="${REPO#ghcr.io/}"
  # The package is private, so an anonymous token is not enough — this reuses
  # whatever GHCR credential the pulling seat has. Without one, warn and carry
  # on rather than block a deploy that would have worked.
  BASIC="$(python3 - "$HOME/.docker/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("auths", {}).get("ghcr.io", {}).get("auth", ""))
except Exception:
    print("")
PY
)"
  TOKEN="$(curl -fsSL ${BASIC:+-H "Authorization: Basic ${BASIC}"} \
           "https://ghcr.io/token?scope=repository:${IMG_PATH}:pull&service=ghcr.io" \
           | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
  if [[ -z "$TOKEN" ]]; then
    echo "    skipped (no GHCR credential here — cannot verify)" >&2
  else
    # Accept manifest lists and OCI indexes as well as single manifests: a
    # registry answers 404, not 406, when the tag exists but no Accept type
    # matches, so a missing index type reads as "never pushed".
    CODE="$(curl -sL -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" \
            -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
            "https://ghcr.io/v2/${IMG_PATH}/manifests/${TAG}" || echo 000)"
    if [[ "$CODE" != "200" ]]; then
      echo "ERROR: ${REPO}:${TAG} is not in the registry (HTTP ${CODE})." >&2
      echo "       Has the GitHub build finished? Check the Actions tab." >&2
      exit 1
    fi
    echo "    ok"
  fi
fi

echo "==> helm upgrade ${RELEASE} -n ${NAMESPACE} --reuse-values --set image.tag=${TAG}"
helm upgrade "$RELEASE" "$HERE" -n "$NAMESPACE" \
  --reuse-values --set image.tag="$TAG" --atomic --cleanup-on-fail

echo "==> Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Live: https://rachelfreeman.art"
kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/instance="${RELEASE}"
