#!/usr/bin/env bash
# ONE-TIME migration: move the Traefik HelmChartConfig from the `duckdns` Helm
# release to the `traefik` release, without Traefik ever losing its config.
#
# WHY THIS IS DELICATE. The object being moved is the only thing teaching
# Traefik about the `duckdns` ACME certresolver. If it ever vanishes from the
# API server — even for a moment — k3s's helm-controller redeploys Traefik with
# stock values, the certresolver disappears, and every HTTPS host in the cluster
# starts serving a self-signed cert until it is put back. So the object is never
# deleted and re-created; only its Helm bookkeeping annotations change.
#
# THE TRICK: `helm.sh/resource-policy: keep` is read off the LIVE object, not
# the template. Annotating it directly is enough to make Helm skip the delete
# when the template disappears from the duckdns chart.
#
# Sequence:
#   1. Annotate live object `helm.sh/resource-policy: keep`
#   2. helm upgrade duckdns  (template already removed from that chart in git)
#      -> Helm logs "Skipping delete ... resource-policy: keep", object survives
#   3. Re-stamp meta.helm.sh/release-name -> traefik, drop the keep annotation
#   4. helm upgrade --install traefik  <- this one DOES redeploy Traefik
#
# Steps 1-3 cause no redeploy at all: valuesContent is untouched, and
# helm-controller only reacts to that. Step 4 is the real change (access logs +
# the updateStrategy fix) and is a genuine cluster-wide ingress outage.
#
# Safe to re-run: every step checks its own postcondition first.

set -euo pipefail

RELEASE="${RELEASE:-traefik}"
NAMESPACE="${NAMESPACE:-infra}"
FROM_RELEASE="${FROM_RELEASE:-duckdns}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DUCKDNS_DIR="${HERE}/../duckdns"

HCC_NAME="$(awk '/^  name:/ {print $2; exit}' <(helm template "$RELEASE" "$HERE" -n "$NAMESPACE"))"
HCC_NS="$(awk '/^  namespace:/ {print $2; exit}' <(helm template "$RELEASE" "$HERE" -n "$NAMESPACE"))"
HCC_NAME="${HCC_NAME:-traefik}"
HCC_NS="${HCC_NS:-kube-system}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> handover target: helmchartconfig/${HCC_NAME} in ${HCC_NS}"

if ! kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" >/dev/null 2>&1; then
  echo "no live HelmChartConfig — nothing to hand over. Run ./upgrade.sh instead."
  exit 0
fi

owner="$(kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" \
  -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
echo "==> current Helm owner: ${owner:-<none>}"

if [[ "$owner" == "$RELEASE" ]]; then
  echo "already owned by '${RELEASE}' — handover done. Run ./upgrade.sh to apply config."
  exit 0
fi

if [[ -n "$owner" && "$owner" != "$FROM_RELEASE" ]]; then
  echo "refusing: owned by unexpected release '${owner}' (expected '${FROM_RELEASE}')"
  exit 1
fi

# Record what Traefik looks like now, so we can prove steps 1-3 changed nothing.
POD_BEFORE="$(kubectl -n "$HCC_NS" get pods -l app.kubernetes.io/name=traefik \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo '<none>')"
echo "==> traefik pod before: ${POD_BEFORE}"

# --- 1. Make the object survive leaving the duckdns chart -------------------
echo "==> [1/4] annotating helm.sh/resource-policy=keep"
kubectl annotate --overwrite helmchartconfig "$HCC_NAME" -n "$HCC_NS" \
  helm.sh/resource-policy=keep >/dev/null

# --- 2. Let duckdns release it ---------------------------------------------
if [[ "$owner" == "$FROM_RELEASE" ]]; then
  if grep -rqs 'kind: HelmChartConfig' "${DUCKDNS_DIR}/templates/"; then
    echo "refusing: ${DUCKDNS_DIR}/templates still renders a HelmChartConfig."
    echo "          Remove it from that chart first, or duckdns will fight this release."
    exit 1
  fi
  echo "==> [2/4] helm upgrade ${FROM_RELEASE} (drops the template, keeps the object)"
  DUCKDNS_VALUES=(-f "${DUCKDNS_DIR}/values.yaml")
  [[ -f "${DUCKDNS_DIR}/values.local.yaml" ]] && DUCKDNS_VALUES+=(-f "${DUCKDNS_DIR}/values.local.yaml")
  helm upgrade "$FROM_RELEASE" "$DUCKDNS_DIR" -n "$NAMESPACE" "${DUCKDNS_VALUES[@]}"

  kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" >/dev/null 2>&1 || {
    echo "FATAL: the HelmChartConfig was deleted despite resource-policy=keep."
    echo "       Traefik is about to lose its certresolver. Re-apply immediately:"
    echo "         helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
    exit 1
  }
  echo "    object survived"
else
  echo "==> [2/4] skipped — already orphaned"
fi

# --- 3. Re-stamp ownership --------------------------------------------------
echo "==> [3/4] re-stamping ownership to release '${RELEASE}'"
kubectl label --overwrite helmchartconfig "$HCC_NAME" -n "$HCC_NS" \
  app.kubernetes.io/managed-by=Helm >/dev/null
kubectl annotate --overwrite helmchartconfig "$HCC_NAME" -n "$HCC_NS" \
  meta.helm.sh/release-name="$RELEASE" \
  meta.helm.sh/release-namespace="$NAMESPACE" >/dev/null
# Drop the keep policy: from here on this chart manages the object normally, and
# a lingering `keep` would silently defeat a future intentional removal.
kubectl annotate helmchartconfig "$HCC_NAME" -n "$HCC_NS" \
  helm.sh/resource-policy- >/dev/null 2>&1 || true

POD_AFTER="$(kubectl -n "$HCC_NS" get pods -l app.kubernetes.io/name=traefik \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo '<none>')"
if [[ "$POD_BEFORE" == "$POD_AFTER" ]]; then
  echo "    traefik pod unchanged (${POD_AFTER}) — no outage so far, as intended"
else
  echo "    NOTE: traefik pod changed ${POD_BEFORE} -> ${POD_AFTER}"
fi

# --- 4. Hand off to the normal path ----------------------------------------
echo
echo "==> [4/4] handover complete. The config change itself is a separate,"
echo "    deliberate step — it redeploys Traefik and takes ingress down briefly:"
echo
echo "        ${HERE}/upgrade.sh"
echo
