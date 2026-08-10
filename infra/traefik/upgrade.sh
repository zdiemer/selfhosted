#!/usr/bin/env bash
# Apply the current chart to the `traefik` release — the cluster's whole Traefik
# config overlay.
#
# READ THIS BEFORE RUNNING. helm-controller redeploys Traefik whenever
# `valuesContent` changes, and `updateStrategy: Recreate` (plus a ReadWriteOnce
# acme.json volume) means the old pod is fully gone before the new one starts.
# That is a cluster-wide ingress OUTAGE of roughly 10-30s: every host in the
# cluster, every repo, not just this one.
#
# Issued certs survive — acme.json lives on the PVC — so this is downtime, not a
# re-issue, and it does not spend Let's Encrypt rate limit.
#
# The script diffs live vs rendered and refuses to proceed silently. Set
# YES=1 to skip the prompt (for non-interactive runs you have already reviewed).

set -euo pipefail

RELEASE="${RELEASE:-traefik}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

render() { helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"; }
# The chart renders more than one document now (the edge-ratelimit Middleware
# joined the HelmChartConfig), and everything below that parses rendered YAML
# line-by-line assumes exactly one. Scope those to the one template they mean.
render_hcc() { render -s templates/helmchartconfig.yaml; }

HCC_NAME="$(render_hcc | awk '/^  name:/ {print $2; exit}')"
HCC_NS="$(render_hcc | awk '/^  namespace:/ {print $2; exit}')"
HCC_NAME="${HCC_NAME:-traefik}"
HCC_NS="${HCC_NS:-kube-system}"

# One namespace per project; created manually, never chart-managed.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# The config used to belong to the duckdns release. If it still does, this
# script would fail on "invalid ownership metadata" — point at the migration
# rather than leaving you to decode Helm's error.
if kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" >/dev/null 2>&1; then
  owner="$(kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  if [[ -n "$owner" && "$owner" != "$RELEASE" ]]; then
    echo "helmchartconfig/${HCC_NAME} still belongs to helm release '${owner}'."
    echo "Run ./handover.sh first — it moves ownership here without an outage."
    exit 1
  fi
fi

# The valuesContent is a YAML string, so neither `helm lint` nor the API server
# will catch a typo inside it — a malformed overlay is simply ignored by
# helm-controller, and Traefik silently keeps its old config or drops to
# defaults. Parse it here, where the failure is loud.
render_hcc | python3 -c '
import sys, yaml
doc = yaml.safe_load(sys.stdin)
vc  = doc["spec"]["valuesContent"]
v   = yaml.safe_load(vc)
assert v["logs"]["access"]["fields"]["headers"]["defaultmode"] == "drop", \
    "access-log header defaultmode must stay drop (keeps cookies/Authorization out of logs)"
if v.get("updateStrategy", {}).get("type") == "Recreate":
    assert v["updateStrategy"].get("rollingUpdate") is None, \
        "Recreate strategy cannot carry rollingUpdate"
print("==> valuesContent parses; safety assertions pass")
' || { echo "rendered valuesContent is not valid — refusing to apply"; exit 1; }

# If the websecure entrypoint references the edge-ratelimit middleware, the
# Middleware object must ship in the same render — an entrypoint naming a
# missing middleware errors EVERY router on websecure, which is the one
# failure mode worse than an outage window.
if render_hcc | grep -q 'edge-ratelimit@kubernetescrd'; then
  render | grep -q 'name: edge-ratelimit' \
    || { echo "entrypoint references edge-ratelimit but the chart does not render the Middleware — refusing to apply"; exit 1; }
fi

# Say plainly whether Traefik is about to restart, and show exactly what changes.
REDEPLOY=0
if kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" >/dev/null 2>&1; then
  live="$(kubectl get helmchartconfig "$HCC_NAME" -n "$HCC_NS" -o jsonpath='{.spec.valuesContent}')"
  rendered="$(render_hcc | awk '/valuesContent:/{f=1;next} f' | sed 's/^    //')"
  if [[ "$live" != "$rendered" ]]; then
    REDEPLOY=1
    echo
    echo "==> Traefik config CHANGED — helm-controller will redeploy Traefik."
    echo "    Expect a brief cluster-wide ingress outage (all hosts, all repos)."
    echo
    diff <(echo "$live") <(echo "$rendered") || true
    echo
    if [[ "${YES:-0}" != "1" ]]; then
      read -r -p "Proceed? [y/N] " ans
      [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted"; exit 1; }
    fi
  else
    echo "==> Traefik config unchanged — no redeploy expected."
  fi
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

if [[ "$REDEPLOY" == "1" ]]; then
  # helm-controller runs the actual Traefik upgrade as a Job, asynchronously —
  # `helm upgrade` returning says nothing about Traefik itself. Wait for the
  # proxy to come back rather than declaring success while ingress is still down.
  echo "==> waiting for Traefik to come back"
  sleep 5
  kubectl -n "$HCC_NS" rollout status deployment/traefik --timeout=180s

  echo "==> verifying the config actually landed"
  kubectl -n "$HCC_NS" get deploy traefik \
    -o jsonpath='    updateStrategy: {.spec.strategy.type}{"\n"}'
  kubectl -n "$HCC_NS" get pods -l app.kubernetes.io/name=traefik \
    -o jsonpath='    pod: {.items[0].metadata.name}{"\n"}'
fi

echo "==> access log sample (empty until the next request arrives)"
kubectl -n "$HCC_NS" logs deploy/traefik --tail=3 2>/dev/null || true
