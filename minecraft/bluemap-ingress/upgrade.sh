#!/usr/bin/env bash
# Apply the BlueMap Ingress.
#
# This chart routes to a Service owned by the upstream itzg/minecraft chart
# (release `mc`), so it pre-flights that the target exists and speaks the
# expected port before touching anything. That's the whole mitigation for the
# cross-chart coupling: if the minecraft chart renames its service, this fails
# here, loudly, instead of quietly serving 503s to map.zachd.duckdns.org.
#
# THE RELEASE IS NAMED `bluemap`, NOT `bluemap-ingress`. The live object was
# created by `kubectl apply -f minecraft/bluemap-ingress.yaml` and is named
# `bluemap`; naming the release to match lets Helm adopt it in place rather
# than creating a second Ingress for the same host and deleting the first.
#
# Nothing here restarts the Minecraft server — this chart owns one Ingress and
# never touches the `mc` release.

set -euo pipefail

RELEASE="${RELEASE:-bluemap}"
# The Ingress must live beside the Service it points at, so this is the
# minecraft release's namespace, not one of our own.
NAMESPACE="${NAMESPACE:-minecraft}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "${HERE}/values.local.yaml" ]] && VALUE_ARGS+=(-f "${HERE}/values.local.yaml")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

read_value() { helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" | awk "$1"; }
HOST="$(read_value '/^    - host:/ {gsub(/"/,"",$3); print $3; exit}')"
SVC="$(read_value '/^                name:/ {print $2; exit}')"
PORT="$(read_value '/^                  number:/ {print $2; exit}')"
NAME="$(read_value '/^  name:/ {print $2; exit}')"

echo "==> Pre-flight: ${HOST} -> ${SVC}:${PORT} in ${NAMESPACE}"

if ! kubectl get svc "$SVC" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "FAIL: service ${SVC} not found in ${NAMESPACE}."
  echo "      It belongs to the upstream itzg/minecraft chart (release 'mc') —"
  echo "      either that isn't installed, or it renamed the service and"
  echo "      target.service here is now stale."
  exit 1
fi

if ! kubectl get svc "$SVC" -n "$NAMESPACE" -o jsonpath='{.spec.ports[*].port}' | tr ' ' '\n' | grep -qx "$PORT"; then
  echo "FAIL: service ${SVC} does not expose port ${PORT}."
  echo "      It exposes: $(kubectl get svc "$SVC" -n "$NAMESPACE" -o jsonpath='{.spec.ports[*].port}')"
  echo "      Update target.port in values.yaml to match."
  exit 1
fi
echo "    ok: ${SVC}:${PORT} exists"

# The live Ingress predates this chart — it was a loose `kubectl apply -f`, so it
# carries no Helm ownership at all. Helm refuses to take over a resource it
# didn't create ("invalid ownership metadata"), so stamp its bookkeeping on the
# first run. Deleting and recreating instead would drop the route for a few
# seconds; adopting in place doesn't.
if kubectl get ingress "$NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  owner="$(kubectl get ingress "$NAME" -n "$NAMESPACE" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  if [[ -z "$owner" ]]; then
    echo "==> adopting pre-existing ingress/${NAME} (was a bare kubectl apply)"
    kubectl label --overwrite ingress "$NAME" -n "$NAMESPACE" \
      app.kubernetes.io/managed-by=Helm >/dev/null
    kubectl annotate --overwrite ingress "$NAME" -n "$NAMESPACE" \
      meta.helm.sh/release-name="$RELEASE" \
      meta.helm.sh/release-namespace="$NAMESPACE" >/dev/null
  elif [[ "$owner" != "$RELEASE" ]]; then
    echo "refusing: ingress/${NAME} already belongs to helm release '${owner}'"
    exit 1
  fi
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" --atomic --cleanup-on-fail

# Prove Traefik actually routes the Host header, rather than trusting that the
# object exists. A 502 here usually means BlueMap isn't enabled in the pod
# (accept-download: true in core.conf) rather than anything wrong with routing.
echo "==> Verifying Traefik routes Host: ${HOST}"
TRAEFIK_IP="$(kubectl get svc traefik -n kube-system -o jsonpath='{.spec.clusterIP}')"
CODE="$(kubectl run "bluemap-probe-$$" -n "$NAMESPACE" --rm -i --restart=Never --quiet \
  --image=curlimages/curl:8.11.1 --command -- \
  curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
  --resolve "${HOST}:443:${TRAEFIK_IP}" "https://${HOST}/" 2>/dev/null || echo "000")"

case "$CODE" in
  000|404) echo "    WARN: Traefik answered ${CODE} for ${HOST} — routing is NOT working"; exit 1 ;;
  502|503) echo "    WARN: Traefik answered ${CODE} — routing works, but BlueMap isn't serving."
           echo "          Check accept-download: true in /data/config/bluemap/core.conf."; exit 1 ;;
  *)       echo "    ok: Traefik answered ${CODE} for ${HOST} (routing works)" ;;
esac

echo "==> Ingress"
kubectl get ingress "$NAME" -n "$NAMESPACE"
