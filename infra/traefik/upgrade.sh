#!/usr/bin/env bash
# Apply the current chart to the `traefik` release — the cluster's whole Traefik
# config overlay.
#
# READ THIS BEFORE RUNNING. helm-controller redeploys Traefik whenever
# `valuesContent` changes. That used to mean a cluster-wide ingress outage of
# 10-30s, because a ReadWriteOnce acme.json forced one replica and a `Recreate`
# rollout. It does not any more: issuance moved to infra/traefik-certs, so
# Traefik is stateless, runs three spread replicas, and rolls with
# maxUnavailable 0.
#
# WHAT CAN STILL GO WRONG is quieter. Traefik now gets its certificate from a
# Secret; if that Secret is missing it does not fail, it serves its own
# self-signed default on every host — a browser warning everywhere, with
# nothing crashing to tell you. So this script refuses to apply unless the
# Secret exists. Run infra/traefik-certs/seed.sh first on a fresh cutover.
#
# The script diffs live vs rendered and refuses to proceed silently. Set
# YES=1 to skip the prompt (for non-interactive runs you have already reviewed).

set -euo pipefail

RELEASE="${RELEASE:-traefik}"
NAMESPACE="${NAMESPACE:-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# The CrowdSec bouncer key comes from 1Password into memory and never onto a
# disk. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# "$@" is load-bearing: without it `render -s templates/...` below silently
# drops the -s and renders the whole chart. render_hcc was added precisely to
# scope the YAML assertions to one document and never did, because the flag
# never reached helm — the assertions then failed on "expected a single
# document" the first time a second template joined the chart. Same shape as
# the strategy keys elsewhere in this repo: a fix that reads as applied and
# isn't.
# `-f <(sv_fd)` lives INSIDE the function, not in VALUE_ARGS: render() is called
# four times below, and a process substitution evaluated once would be an
# exhausted pipe by the second call. Here each invocation opens a fresh one.
render() { helm template "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) "$@"; }
# The chart renders more than one document now (the edge-ratelimit Middleware
# joined the HelmChartConfig), and everything below that parses rendered YAML
# line-by-line assumes exactly one. Scope those to the one template they mean.
render_hcc() { render -s templates/helmchartconfig.yaml; }

HCC_NAME="$(render_hcc | awk '/^  name:/ {print $2; exit}')"
HCC_NS="$(render_hcc | awk '/^  namespace:/ {print $2; exit}')"
HCC_NAME="${HCC_NAME:-traefik}"
HCC_NS="${HCC_NS:-kube-system}"

# The default certificate must exist BEFORE Traefik is told to read it.
# Applying without it does not fail — Traefik falls back to a self-signed cert
# and every host in the cluster starts throwing browser warnings, with no error
# anywhere to point at. Cheaper to refuse here.
CERT_SECRET="$(python3 -c "
import yaml; print(yaml.safe_load(open('${VALUES}'))['defaultCertificate']['secretName'])")"
if ! kubectl -n "$HCC_NS" get secret "$CERT_SECRET" >/dev/null 2>&1; then
  cat >&2 <<EOF
FAIL: Secret ${CERT_SECRET} does not exist in ${HCC_NS}.

Traefik reads its default certificate from that Secret. Applying this config
without it would leave every host on Traefik's self-signed default.

  infra/traefik-certs/seed.sh        # copy the cert out of the live acme.json
  infra/traefik-certs/upgrade.sh     # install the renewal CronJob

then re-run this.
EOF
  exit 1
fi
echo "==> default certificate (${CERT_SECRET}):"
kubectl -n "$HCC_NS" get secret "$CERT_SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d \
  | openssl x509 -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/    /'

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
# The default certificate is the whole reason Traefik can be stateless. Losing
# this key does not fail anything — Traefik just serves a self-signed cert on
# every host — so assert it is present rather than trusting the template.
assert v.get("tlsStore", {}).get("default", {}).get("defaultCertificate", {}).get("secretName"), \
    "tlsStore.default.defaultCertificate.secretName is required; without it every host gets a self-signed cert"
# Persistence coming back would silently reintroduce the RWO volume that forced
# one replica and made every change here an outage.
assert v.get("persistence", {}).get("enabled") is False, \
    "persistence must stay disabled; a PVC here re-pins Traefik to one node"
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
    echo "    This is a rolling update now (3 replicas, maxUnavailable 0), not"
    echo "    the cluster-wide outage it used to be. Still every route in, so"
    echo "    read the diff."
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
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail

if [[ "$REDEPLOY" == "1" ]]; then
  # helm-controller runs the actual Traefik upgrade as a Job, asynchronously —
  # `helm upgrade` returning says nothing about Traefik itself.
  #
  # `sleep 5` used to stand in for that, and it is not enough: the Job takes
  # tens of seconds to start, so `rollout status` ran against a Deployment that
  # had not been touched yet, found it perfectly rolled out, and reported
  # success. The cutover to three replicas "passed" this check while Traefik
  # was still a single Recreate pod on the old config.
  #
  # Wait for the Job first, then the Deployment.
  echo "==> waiting for helm-controller to apply the config"
  kubectl -n "$HCC_NS" wait --for=condition=complete job/helm-install-traefik --timeout=300s \
    || echo "    (no helm-install job completed in time; checking the deployment anyway)" >&2
  echo "==> waiting for Traefik to come back"
  kubectl -n "$HCC_NS" rollout status deployment/traefik --timeout=300s

  echo "==> verifying the config actually landed"
  kubectl -n "$HCC_NS" get deploy traefik \
    -o jsonpath='    replicas: {.spec.replicas}  strategy: {.spec.strategy.type}{"\n"}'
  kubectl -n "$HCC_NS" get pods -l app.kubernetes.io/name=traefik \
    -o jsonpath='    pod: {.items[0].metadata.name}{"\n"}'
fi

echo "==> access log sample (empty until the next request arrives)"
kubectl -n "$HCC_NS" logs deploy/traefik --tail=3 2>/dev/null || true
