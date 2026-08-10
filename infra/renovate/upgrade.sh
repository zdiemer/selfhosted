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
LOCAL_VALUES="${HERE}/values.local.yaml"

# Materialize values.local.yaml from 1Password when it's missing and a template
# exists. Convenience only: values.local.yaml is still the contract, so this
# no-ops without `op` — e.g. in the claude-workspace pod, which is fed by
# `scripts/secrets.sh publish` instead. See values.local.tpl.yaml.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES} (needs github.pat — see values.yaml comment for the PAT spec)"
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f "$LOCAL_VALUES"

echo "==> CronJob state"
kubectl -n "$NAMESPACE" get cronjob "$RELEASE"
echo
echo "Next run per schedule above. Manual run:"
echo "  kubectl -n ${NAMESPACE} create job --from=cronjob/${RELEASE} ${RELEASE}-manual"
