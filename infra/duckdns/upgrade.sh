#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running duckdns release.
#
# This owns the cluster's public DNS (the updater CronJob) and the DuckDNS token
# that Traefik's ACME certresolver reads. Breaking the token Secret stops every
# cert in the cluster renewing, so it moves deliberately: it adopts the
# pre-existing kube-system Secret rather than fighting it, and it force-runs the
# updater so a bad token fails in front of you.
#
# The Traefik config itself lives in infra/traefik — this script no longer
# touches it, and no longer causes an ingress outage.

set -euo pipefail

RELEASE="${RELEASE:-duckdns}"
NAMESPACE="${NAMESPACE:-infra}"
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

VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES} — copy values.local.yaml.example and add the DuckDNS token"
  exit 1
fi

# Read Traefik's namespace off the rendered Secret rather than values.yaml, so a
# values.local.yaml override still points the adoption at the right place. The
# second Secret copy is the only thing this chart still puts outside its own
# namespace — the Traefik config itself moved to infra/traefik.
TRAEFIK_NS="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" \
  -s templates/secret.yaml 2>/dev/null \
  | awk '/^  namespace:/ {print $2}' | grep -v "^${NAMESPACE}$" | head -1)"
TRAEFIK_NS="${TRAEFIK_NS:-kube-system}"

# One namespace per project; created manually, never chart-managed.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# The kube-system token Secret and the Traefik HelmChartConfig both predate this
# chart — they were hand-applied back when this config lived in the talaria
# repo. Helm refuses to take over a resource it didn't create ("invalid
# ownership metadata"), so stamp its bookkeeping on first run. This is a no-op
# on every subsequent upgrade.
adopt() {
  local kind="$1" name="$2" ns="$3" owner
  kubectl get "$kind" "$name" -n "$ns" >/dev/null 2>&1 || return 0

  owner="$(kubectl get "$kind" "$name" -n "$ns" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  [[ "$owner" == "$RELEASE" ]] && return 0
  if [[ -n "$owner" ]]; then
    echo "refusing: ${kind}/${name} in ${ns} already belongs to helm release '${owner}'"
    exit 1
  fi

  echo "==> adopting pre-existing ${kind}/${name} in ${ns} into release ${RELEASE}"
  kubectl label --overwrite "$kind" "$name" -n "$ns" \
    app.kubernetes.io/managed-by=Helm >/dev/null
  kubectl annotate --overwrite "$kind" "$name" -n "$ns" \
    meta.helm.sh/release-name="$RELEASE" \
    meta.helm.sh/release-namespace="$NAMESPACE" >/dev/null
}

adopt secret duckdns-token "$TRAEFIK_NS"

# The Traefik HelmChartConfig used to be adopted here too. It now belongs to the
# `traefik` release (infra/traefik). If this chart still claims it, the split
# was left half-done — say so, because a duckdns upgrade would then fight the
# traefik release over the same object on every run.
if kubectl get helmchartconfig traefik -n "$TRAEFIK_NS" >/dev/null 2>&1; then
  hcc_owner="$(kubectl get helmchartconfig traefik -n "$TRAEFIK_NS" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  if [[ "$hcc_owner" == "$RELEASE" ]]; then
    echo "==> WARNING: helmchartconfig/traefik still belongs to release '${RELEASE}'."
    echo "    The Traefik config moved to infra/traefik. Run its handover script:"
    echo "        ${HERE}/../traefik/handover.sh"
    echo "    Continuing — this upgrade will not touch that object."
  fi
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

# The CronJob only proves itself on its own schedule, which is up to 5 minutes
# away. Force one now so a broken token fails here, in front of you, rather
# than quietly at 3am.
echo "==> Test run of the updater"
$K delete job "${RELEASE}-updater-verify" --ignore-not-found >/dev/null 2>&1 || true
$K create job "${RELEASE}-updater-verify" --from="cronjob/${RELEASE}-updater"
$K wait --for=condition=complete "job/${RELEASE}-updater-verify" --timeout=90s
$K logs "job/${RELEASE}-updater-verify"
$K delete job "${RELEASE}-updater-verify" >/dev/null

echo "==> CronJob"
$K get cronjob "${RELEASE}-updater"
