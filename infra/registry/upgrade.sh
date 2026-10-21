#!/usr/bin/env bash
# Apply the current chart to the running registry release.
#
# The deploy check is a real push and pull, not a pod status: a registry that
# is Running and answering 401 to everything looks healthy from `kubectl get
# pods`. So after the rollout this pushes a one-layer image through the public
# host with the vault credential (the path build.sh uses) and then fetches its
# manifest back through the ClusterIP with the same credential (the path every
# node's containerd uses). Both have to work, or nothing downstream will.

set -euo pipefail

RELEASE="${RELEASE:-registry}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Secrets are resolved from 1Password into memory for the life of this run and
# never written to a disk. Each helm call spells out its own `-f <(sv_fd)`: that
# fd is a pipe, readable once. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

python3 "$HERE/../../scripts/internal-pki.py"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

HOST="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["registry"]["host"])')"
REGISTRY_USER="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["registry"]["auth"]["username"])')"
REGISTRY_PASS="$(sv_fd | python3 -c 'import sys,yaml; print(yaml.safe_load(sys.stdin)["registry"]["auth"]["password"])')"

# A push exercises Traefik, the wildcard cert, the htpasswd and the blob store
# in one go; an anonymous 401 proves the gate is actually closed.
echo "==> Gate: anonymous GET /v2/ must be refused"
code="$(curl -sS -o /dev/null -w '%{http_code}' "https://${HOST}/v2/")"
[[ "$code" == "401" ]] || { echo "    !! expected 401, got ${code}"; exit 1; }
echo "    401, as it should be"

echo "==> Push a probe image through https://${HOST}"
PROBE="${HOST}/zdiemer/registry-probe:$(date -u +%Y%m%d%H%M%S)"
# A throwaway docker config dir on tmpfs, so the credential never lands on ext4.
cfg_dir="$(mktemp -d -p /dev/shm 2>/dev/null || mktemp -d -p "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}")"
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$HOST" "$(printf '%s:%s' "$REGISTRY_USER" "$REGISTRY_PASS" | base64 -w0)" >"${cfg_dir}/config.json"
ctx="$(mktemp -d)"
trap 'rm -rf "$cfg_dir" "$ctx"' EXIT
printf 'FROM scratch\nCOPY probe /probe\n' >"${ctx}/Dockerfile"
echo "registry probe" >"${ctx}/probe"
if command -v buildctl >/dev/null; then
  DOCKER_CONFIG="$cfg_dir" buildctl build --frontend dockerfile.v0 \
    --local context="$ctx" --local dockerfile="$ctx" \
    --output "type=image,\"name=${PROBE}\",push=true" >/dev/null
elif command -v docker >/dev/null; then
  docker --config "$cfg_dir" build -q -t "$PROBE" "$ctx" >/dev/null
  docker --config "$cfg_dir" push "$PROBE" >/dev/null
else
  echo "    (no buildctl or docker; skipping the push)"
  PROBE=""
fi
if [[ -n "$PROBE" ]]; then
  echo "    pushed ${PROBE}"
fi

echo "==> Node config and CRI pulls (sequential maintenance jobs)"
python3 "$HERE/apply-node-config.py" "$PROBE"

echo "==> Ingress"
$K get ingress "$RELEASE"
