#!/usr/bin/env bash
# Install/upgrade Grafana Alloy from the upstream chart, with our values.
#
# Values-only project (like infra/headlamp and minecraft/): there is no chart
# here, so this adds the upstream repo first. It also builds the Grafana Cloud
# credentials Secret out of values.local.yaml, because the upstream chart has no
# way to create one and the token must not end up in a ConfigMap.
#
# Nothing here touches ingress. Alloy only reads.

set -euo pipefail

RELEASE="${RELEASE:-alloy}"
NAMESPACE="${NAMESPACE:-alloy}"
CHART="${CHART:-grafana/alloy}"
SECRET="alloy-grafana-cloud"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES}"
  echo "  cp values.local.yaml.example values.local.yaml   # then add the Grafana Cloud token"
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# Pull the five credential fields out of values.local.yaml. They go into a
# Secret rather than into the chart values so they never land in the rendered
# ConfigMap, which is world-readable to anyone with namespace access.
read_cred() {
  python3 -c "
import sys, yaml
d = yaml.safe_load(open('${LOCAL_VALUES}')) or {}
v = (d.get('grafanaCloud') or {}).get('$1', '')
if not v or 'XXX' in str(v) or str(v).startswith('glc_00000'):
    sys.exit(1)
print(v)
"
}

for f in lokiUrl lokiUser promUrl promUser token; do
  if ! read_cred "$f" >/dev/null; then
    echo "FAIL: grafanaCloud.${f} is unset or still the example placeholder in ${LOCAL_VALUES}"
    exit 1
  fi
done

echo "==> writing Secret ${SECRET} in ${NAMESPACE}"
kubectl create secret generic "$SECRET" -n "$NAMESPACE" \
  --from-literal=lokiUrl="$(read_cred lokiUrl)" \
  --from-literal=lokiUser="$(read_cred lokiUser)" \
  --from-literal=promUrl="$(read_cred promUrl)" \
  --from-literal=promUser="$(read_cred promUser)" \
  --from-literal=token="$(read_cred token)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "==> helm repo add grafana"
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null

echo "==> helm upgrade --install ${RELEASE} ${CHART} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES"

kubectl -n "$NAMESPACE" rollout status daemonset/"${RELEASE}" --timeout=180s

# A bad token does not stop the pod — Alloy starts fine and simply fails every
# push, so "Running" proves nothing. Read the log for the actual answer.
echo "==> checking for delivery errors (10s)"
sleep 10
if kubectl -n "$NAMESPACE" logs daemonset/"$RELEASE" --tail=200 2>/dev/null \
   | grep -iE 'final error sending batch|401|403|unauthorized' | head -5; then
  echo "    ^ credentials or endpoint look wrong — check values.local.yaml"
else
  echo "    no delivery errors"
fi

echo "==> what is being shipped"
kubectl -n "$NAMESPACE" logs daemonset/"$RELEASE" --tail=200 2>/dev/null \
  | grep -c 'tail routine' || true
cat <<EOF

Verify in Grafana Cloud -> Explore:

    {cluster="home-k3s", app="traefik"} | json | DownstreamStatus >= 400

and that a pod with NO external ingress is absent:

    {cluster="home-k3s", namespace="default"}     # talaria workers: expect nothing

Watch the monthly projection at grafana.com -> your stack -> Billing/Usage.
Budget is 50 GB/month; this allowlist should land near 0.3-1 GB.
EOF
