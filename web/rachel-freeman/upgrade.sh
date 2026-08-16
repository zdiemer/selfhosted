#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running rachel-freeman
# release.
#
# Run build.sh first if the app source changed — this only rolls the deployment
# onto whatever image.tag says.

set -euo pipefail

RELEASE="${RELEASE:-rachel-freeman}"
NAMESPACE="${NAMESPACE:-rachel}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# The secrets are resolved from 1Password into memory for the life of this run
# and never written to disk. Each helm call spells out its own `-f <(sv_fd)`
# rather than carrying it in VALUE_ARGS: that fd is a pipe, readable once, so a
# shared one would hand the second reader an empty values file. See
# scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "no secrets resolved from 1Password — copy values.local.yaml.example and fill it in"
  echo "  check with:  ./scripts/secrets.sh check web/rachel-freeman"
  exit 1
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --atomic --cleanup-on-fail

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}-postgres" --timeout=180s
# Longer than postgres: on a cold database the app's first boot runs Payload's
# schema push before it will answer /healthz.
$K rollout status "deployment/${RELEASE}" --timeout=300s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
