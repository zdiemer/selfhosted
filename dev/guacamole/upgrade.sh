#!/usr/bin/env bash
# Apply the chart to the guacamole release.

set -euo pipefail

RELEASE="${RELEASE:-guacamole}"
NAMESPACE="${NAMESPACE:-dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Secrets are resolved from 1Password into memory for the life of this run and
# never written to disk. sv_fd is a pipe and readable exactly once, so each helm
# invocation needs its own `-f <(sv_fd)`. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")
K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

# ------------------------------------------------------------------------------
# Verify the thing Guacamole actually connects TO
# ------------------------------------------------------------------------------
# Guacamole comes up healthy whether or not there is anything on the other end,
# so a green rollout says nothing about whether a desktop will load. The two
# ways it fails in practice are a stopped VM (no endpoints) and a running VM
# with Remote Desktop still switched off inside Windows.
echo "==> Connection targets"
helm get values "$RELEASE" -n "$NAMESPACE" --all -o json 2>/dev/null \
  | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for c in v.get("connections") or []:
    name = c.get("name")
    host = c.get("hostname", "")
    port = c.get("port")
    print(f"    {name}: {host}:{port}")
' || true

for svc in $($K get svc -o name 2>/dev/null | grep -- '-rdp$' || true); do
  eps="$($K get endpoints "${svc#service/}" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  if [[ -z "$eps" ]]; then
    echo "    ${svc#service/}: NO ENDPOINTS — the VM is not running."
    echo "        virtctl start <vm> -n ${NAMESPACE}"
  else
    echo "    ${svc#service/}: ${eps}"
  fi
done

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"

HOST="$(helm get values "$RELEASE" -n "$NAMESPACE" --all -o json 2>/dev/null | python3 -c 'import json,sys; print((json.load(sys.stdin).get("ingress") or {}).get("host",""))' 2>/dev/null || true)"
[[ -n "$HOST" ]] && echo && echo "    https://${HOST}  (tailnet only — infra/duckdns is in tailnet mode)"
