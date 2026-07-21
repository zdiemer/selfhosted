#!/usr/bin/env bash
# Apply the buildkit chart.
#
# The one way this deploy fails that helm can't tell you about: rootless
# buildkitd needs to create a user namespace, and Ubuntu 24.04 ships
# kernel.apparmor_restrict_unprivileged_userns=1 which forbids exactly that
# for unconfined processes. So before touching helm, this pre-flights the
# sysctl on every node (best-effort, via tailscale ssh) — and after the
# rollout it proves the oci worker actually initialized, which is the step
# that dies when the sysctl is wrong.

set -euo pipefail

RELEASE="${RELEASE:-buildkit}"
NAMESPACE="${NAMESPACE:-buildkit}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUE_ARGS=(-f "${HERE}/values.yaml")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Best-effort sysctl pre-flight. Warn, don't fail: tailscale may not be up
# (fresh workspace pod), and the pod may well land on a compliant node anyway.
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
  echo "==> Pre-flight: unprivileged-userns sysctl on each node"
  BAD=0
  for NODE in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    VAL="$(tailscale ssh "root@${NODE}" sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo '?')"
    [[ "$VAL" == "0" ]] || { echo "    ${NODE}: kernel.apparmor_restrict_unprivileged_userns=${VAL}"; BAD=1; }
  done
  if [[ "$BAD" == "1" ]]; then
    echo "    WARNING: rootless buildkitd will crashloop on the nodes above."
    echo "    Fix (persistent), per node:"
    echo "      tailscale ssh root@<node> 'printf \"kernel.apparmor_restrict_unprivileged_userns=0\\n\" > /etc/sysctl.d/60-buildkit-userns.conf && sysctl --system >/dev/null'"
  fi
else
  echo "==> Skipping sysctl pre-flight (tailscale not available here)"
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}" --timeout=300s

# The readiness probe already runs this, but run it once more visibly: one
# `oci` worker listed = builds will work.
echo "==> Smoke: buildctl debug workers"
kubectl -n "$NAMESPACE" exec "deploy/${RELEASE}" -- \
  buildctl --addr tcp://127.0.0.1:1234 debug workers
