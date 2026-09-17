#!/usr/bin/env bash
# Build and push the three fsu images from one context.
#
# Unusually for this repo, this chart builds THREE images — fsu-web (the static
# site plus the Breakout jar), fsu-terminal (Cloysta and the MyDS REPL behind
# ttyd) and fsu-pybank (Django 1.9 on Python 2.7). One build context, three
# Dockerfiles, one version number across all of them.
#
# Run ./upgrade.sh afterwards to roll the deployments onto the new images.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPONENTS=(web terminal pybank)

APPVERSION="$(awk -F'"' '/^appVersion:/{print $2; exit}' "${HERE}/Chart.yaml")"
[[ -n "$APPVERSION" ]] || { echo "FAIL: no appVersion in Chart.yaml"; exit 1; }

# ---------------------------------------------------------------------------
# The submodules ARE the build context.
#
# A clone without --recurse-submodules leaves projects/ as four empty
# directories and the build fails somewhere inside a COPY with an opaque
# message. Test a real file in each rather than .git, so a half-finished
# checkout fails here too.
check_source() {
    [[ -f "${HERE}/$1" ]] || {
        echo "FAIL: missing ${HERE}/$1"
        echo "      run: git submodule update --init web/fsu/projects/$2"
        exit 1
    }
}
check_source projects/Cloysta/makefile                    Cloysta
check_source projects/Breakout/src/breakout/Breakout.java Breakout
check_source projects/pybank/manage.py                    pybank
check_source projects/my-data-structure/MyDS.h            my-data-structure

# ---------------------------------------------------------------------------
# Read repository and tag out of values.yaml, never hardcoded here, so the image
# reference has exactly one source of truth. python3 rather than awk because
# these live two levels down in a nested map.
values() {
    python3 -c "
import sys, yaml
v = yaml.safe_load(open('${HERE}/values.yaml'))
print(v['images']['$1']['$2'])
"
}

# Verify the registry over HTTP, using the same credential buildctl forwards.
# This is the buildctl-path equivalent of \`docker manifest inspect\`: without it
# a workspace-pod build would silently skip the immutable-tag check, which is
# the check that matters most (see below).
manifest_exists() {
    local repo="$1" tag="$2" host path auth
    host="${repo%%/*}"
    path="${repo#*/}"
    auth="$(python3 -c "
import json, sys
try:
    a = json.load(open('${HOME}/.docker/config.json'))['auths'].get('${host}', {}).get('auth')
except Exception:
    a = None
sys.stdout.write(a or '')
")"
    [[ -n "$auth" ]] || return 2   # cannot tell; caller decides
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' -I \
        -H "Authorization: Basic ${auth}" \
        -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
        "https://${host}/v2/${path}/manifests/${tag}" || echo 000)"
    [[ "$code" == "200" ]]
}

for c in "${COMPONENTS[@]}"; do
    REPO="$(values "$c" repository)"
    TAG="$(values "$c" tag)"

    # HARD FAIL, not infra/hatch's WARN. Three images telling one story: if they
    # are allowed to disagree, "which one did I forget to bump" becomes a
    # question somebody has to answer at the worst possible moment.
    [[ "$TAG" == "$APPVERSION" ]] || {
        echo "FAIL: images.${c}.tag=${TAG} != Chart.yaml appVersion=${APPVERSION}"
        exit 1
    }

    IMAGE="${REPO}:${TAG}"

    # Immutable tags. imagePullPolicy is IfNotPresent, so pushing over an
    # existing tag does NOT reach any node that already holds it — the cluster
    # keeps serving the old bytes and the rollout looks like it worked.
    if [[ "${FSU_ALLOW_TAG_OVERWRITE:-0}" != "1" ]]; then
        exists=1
        if command -v docker >/dev/null; then
            docker manifest inspect "$IMAGE" >/dev/null 2>&1 && exists=0
        else
            # 0 = present, 1 = absent, 2 = could not tell.
            manifest_exists "$REPO" "$TAG" && exists=0 || exists=$?
        fi
        case "$exists" in
            0) echo "FAIL: ${IMAGE} already exists in the registry."
               echo "      Bump images.*.tag in values.yaml AND version + appVersion in Chart.yaml."
               exit 1 ;;
            2) echo "NOTE: could not check whether ${IMAGE} already exists (no registry credential)." ;;
        esac
    fi

    echo "==> Building ${IMAGE} (Dockerfile.${c})"
    if command -v docker >/dev/null; then
        docker build -f "${HERE}/Dockerfile.${c}" -t "$IMAGE" "$HERE"
        echo "==> Pushing ${IMAGE}"
        docker push "$IMAGE"
    elif command -v buildctl >/dev/null; then
        # Workspace-pod path: remote build on the in-cluster buildkitd, which
        # pushes straight to the registry. Auth is forwarded per-session from
        # ~/.docker/config.json.
        [[ -f "${HOME}/.docker/config.json" ]] || {
            echo "missing ~/.docker/config.json — add the registry credential first"
            echo "(see selfhosted/infra/registry/README.md)"; exit 1; }

        # buildctl has NO -f. --local dockerfile= takes a DIRECTORY and the
        # filename is a frontend option. This is the one line that differs from
        # every other build.sh in this repo, because every other chart has
        # exactly one Dockerfile.
        buildctl build \
            --frontend dockerfile.v0 \
            --local context="$HERE" \
            --local dockerfile="$HERE" \
            --opt filename="Dockerfile.${c}" \
            --output "type=image,\"name=${IMAGE}\",push=true"
    else
        echo "docker or buildctl required"; exit 1
    fi
done

echo "==> Done. Run ./upgrade.sh to roll the deployments onto the new images."
