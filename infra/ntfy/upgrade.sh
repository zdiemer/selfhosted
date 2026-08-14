#!/usr/bin/env bash
# Apply the current chart to the running ntfy release.
#
# The deploy check is about delivery, not about pods: a notification server that
# is Running and rejecting every publish looks identical to a healthy one from
# `kubectl get pods`. So this waits for the seeder to finish, lists the users it
# actually wrote, and then publishes a real message through the public host with
# the publisher credential — which is the only thing that exercises Traefik, the
# ACL and the topic in one go.

set -euo pipefail

RELEASE="${RELEASE:-ntfy}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# Secrets are resolved from 1Password into memory for the life of this run and
# never written to a disk. Each helm call spells out its own `-f <(sv_fd)`: that
# fd is a pipe, readable once, so a shared one would hand the second reader an
# empty values file. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
  "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=300s

POD="$($K get pod -l app.kubernetes.io/instance="${RELEASE}" \
  --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}')"

# The seeder runs alongside the server, so the pod is Ready before the accounts
# exist. Without this wait the user list below races it and prints nothing on a
# first install, which reads as "seeding is broken".
echo "==> Waiting for the user seeder"
for _ in $(seq 1 60); do
  $K logs "$POD" --tail=50 2>/dev/null | grep -q '^\[seed\] done' && break
  sleep 2
done
if ! $K logs "$POD" --tail=50 2>/dev/null | grep -q '^\[seed\] done'; then
  echo "    !! seeder did not finish - accounts may be missing or stale"
  $K logs "$POD" --tail=20 | grep '^\[seed\]' || true
fi

echo "==> Users and access control"
$K exec "$POD" -- ntfy access 2>/dev/null | sed 's/^/    /'

# Publishing through the public URL is the check that matters. Anything short of
# it — a ClusterIP curl, a pod status — passes just as happily when the ingress
# is wrong, the cert is stale, or the ACL denies the publisher.
if [[ -n "${NTFY_SMOKE_USER:-}" && -n "${NTFY_SMOKE_PASS:-}" ]]; then
  BASE="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[0].host}')"
  echo "==> Publishing a test message to https://${BASE}/deploy"
  if curl -fsS -u "${NTFY_SMOKE_USER}:${NTFY_SMOKE_PASS}" \
      -H "Title: ntfy deployed" -H "Tags: white_check_mark" \
      -d "ntfy ${RELEASE} upgraded on $(hostname)" \
      "https://${BASE}/deploy" >/dev/null; then
    echo "    delivered"
  else
    echo "    !! publish FAILED - the server is up but not usable"
    exit 1
  fi
else
  echo "==> Skipping the delivery check (set NTFY_SMOKE_USER / NTFY_SMOKE_PASS)"
fi

echo "==> Ingress"
$K get ingress "$RELEASE"
