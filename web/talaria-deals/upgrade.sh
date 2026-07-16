#!/usr/bin/env bash
# Apply the talaria.deals Ingress.
#
# This chart routes to a Service owned by ANOTHER repo's chart (talaria's), so it
# pre-flights that the target actually exists and speaks the expected port before
# touching anything. That's the whole mitigation for the cross-repo coupling: if
# talaria renames its service, this fails here, loudly, instead of quietly
# serving 503s to talaria.deals.

set -euo pipefail

RELEASE="${RELEASE:-talaria-deals}"
# The Ingress must live beside the Service it points at, so this is talaria's
# namespace, not one of our own.
NAMESPACE="${NAMESPACE:-default}"
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

echo "==> Pre-flight: ${HOST} -> ${SVC}:${PORT} in ${NAMESPACE}"

if ! kubectl get svc "$SVC" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "FAIL: service ${SVC} not found in ${NAMESPACE}."
  echo "      talaria is deployed from ~/Code/talaria — either it isn't installed,"
  echo "      or it renamed this service and target.service here is now stale."
  exit 1
fi

if ! kubectl get svc "$SVC" -n "$NAMESPACE" -o jsonpath='{.spec.ports[*].port}' | tr ' ' '\n' | grep -qx "$PORT"; then
  echo "FAIL: service ${SVC} does not expose port ${PORT}."
  echo "      It exposes: $(kubectl get svc "$SVC" -n "$NAMESPACE" -o jsonpath='{.spec.ports[*].port}')"
  echo "      Update target.port in values.yaml to match."
  exit 1
fi
echo "    ok: ${SVC}:${PORT} exists"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

# Prove Traefik routes the Host header without waiting on Cloudflare. This is the
# in-cluster half working end to end; the tunnel half is dashboard config.
echo "==> Verifying Traefik routes Host: ${HOST}"
TRAEFIK_IP="$(kubectl get svc traefik -n kube-system -o jsonpath='{.spec.clusterIP}')"
CODE="$(kubectl run "talaria-deals-probe-$$" -n "$NAMESPACE" --rm -i --restart=Never --quiet \
  --image=curlimages/curl:8.11.1 --command -- \
  curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
  --resolve "${HOST}:443:${TRAEFIK_IP}" "https://${HOST}/" 2>/dev/null || echo "000")"

case "$CODE" in
  000|404) echo "    WARN: Traefik answered ${CODE} for ${HOST} — routing is NOT working"; exit 1 ;;
  *)       echo "    ok: Traefik answered ${CODE} for ${HOST} (routing works)" ;;
esac

echo "==> Ingress"
kubectl get ingress "$RELEASE" -n "$NAMESPACE"
cat <<EOF

NOTE: the cluster half is done. talaria.deals stays dark until the tunnel knows
about it — in Cloudflare, Zero Trust -> Networks -> Tunnels -> the shared tunnel
-> Public Hostnames, add:

    Hostname:  ${HOST}
    Service:   https://traefik.kube-system.svc.cluster.local:443
    TLS:       No TLS Verify = ON
    Host hdr:  (blank — preserve original)

That auto-creates the proxied DNS record in the talaria.deals zone.
EOF
