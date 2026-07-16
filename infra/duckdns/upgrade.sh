#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running duckdns release.
#
# This owns the cluster's public DNS (the updater CronJob) *and* its TLS (the
# `duckdns` ACME certresolver that every ingress in this repo names). A bad
# upgrade here can take HTTPS down cluster-wide, so it moves deliberately:
# it adopts the pre-existing kube-system objects rather than fighting them, and
# it tells you up front whether Traefik is about to redeploy.

set -euo pipefail

RELEASE="${RELEASE:-duckdns}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES} — copy values.local.yaml.example and add the DuckDNS token"
  exit 1
fi

# Read the Traefik namespace off the rendered manifest rather than values.yaml,
# so a values.local.yaml override still points the adoption at the right place.
TRAEFIK_NS="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" \
  -s templates/traefik-config.yaml 2>/dev/null | awk '/^  namespace:/ {print $2; exit}')"
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
adopt helmchartconfig traefik "$TRAEFIK_NS"

# Traefik redeploys iff valuesContent actually changes, and with a Recreate
# strategy that's a real ingress gap rather than a rolling one. Say so before
# it happens instead of leaving you to guess from the blip.
if kubectl get helmchartconfig traefik -n "$TRAEFIK_NS" >/dev/null 2>&1; then
  live="$(kubectl get helmchartconfig traefik -n "$TRAEFIK_NS" -o jsonpath='{.spec.valuesContent}')"
  rendered="$(helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" \
    -s templates/traefik-config.yaml | awk '/valuesContent:/{f=1;next} f' | sed 's/^    //')"
  if [[ "$live" != "$rendered" ]]; then
    echo "==> NOTE: Traefik config changed — helm-controller will redeploy Traefik."
    echo "    Expect a brief cluster-wide ingress outage. Issued certs survive"
    echo "    (acme.json is on the PVC), so this is downtime, not a re-issue."
    diff <(echo "$live") <(echo "$rendered") || true
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
