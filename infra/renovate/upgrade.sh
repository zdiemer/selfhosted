#!/usr/bin/env bash
# Install/upgrade the Renovate CronJob.
#
# To trigger a run outside the Saturday schedule:
#   kubectl -n renovate create job --from=cronjob/renovate renovate-manual
#   kubectl -n renovate logs -f job/renovate-manual

set -euo pipefail

RELEASE="${RELEASE:-renovate}"
NAMESPACE="${NAMESPACE:-renovate}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# github.pat is resolved from 1Password into memory for the life of this run and
# never written to a disk. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if ! sv_has; then
  echo "no github.pat resolved from 1Password (see values.yaml for the PAT spec)"
  echo "  check with:  ./scripts/secrets.sh check infra/renovate"
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

echo "==> CronJob state"
kubectl -n "$NAMESPACE" get cronjob "$RELEASE"
echo
echo "Next run per schedule above. Manual run:"
echo "  kubectl -n ${NAMESPACE} create job --from=cronjob/${RELEASE} ${RELEASE}-manual"
