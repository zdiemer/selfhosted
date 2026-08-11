#!/usr/bin/env bash
# Apply the current chart to the traefik-certs release.
#
# This chart is what makes infra/traefik stateless, so the ordering between them
# matters and is enforced here rather than remembered:
#
#   1. ./seed.sh                     (once, before the first cutover)
#   2. ./upgrade.sh                  (this — installs the renewal CronJob)
#   3. ./upgrade.sh --run-now        (prove lego works, on a normal day)
#   4. infra/traefik/upgrade.sh      (the cutover; every host's TLS depends on
#                                     the Secret existing by now)
#
# Deploying this chart alone changes nothing user-visible: until infra/traefik
# is cut over, Traefik is still serving its own acme.json and this CronJob just
# maintains a Secret nobody reads yet. That is deliberate — it means the risky
# step is a separate, revertible decision.

set -euo pipefail

RELEASE="${RELEASE:-traefik-certs}"
NAMESPACE="${NAMESPACE:-kube-system}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

RUN_NOW=false
[[ "${1:-}" == "--run-now" ]] && RUN_NOW=true

VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# The DuckDNS token is owned by infra/duckdns, which renders it into kube-system
# for Traefik. This chart only references it. Failing here is much cheaper than
# a CronJob that runs weekly and fails weekly for want of a Secret.
TOKEN_SECRET="$(python3 -c "
import yaml; print(yaml.safe_load(open('${VALUES}'))['tokenSecret']['name'])")"
if ! $K get secret "$TOKEN_SECRET" >/dev/null 2>&1; then
  echo "FAIL: Secret ${TOKEN_SECRET} not found in ${NAMESPACE}."
  echo "      It belongs to infra/duckdns — run that chart's upgrade.sh first."
  exit 1
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" \
  --cleanup-on-fail

echo "==> CronJob"
$K get cronjob "$RELEASE"

CERT_SECRET="$(python3 -c "
import yaml; print(yaml.safe_load(open('${VALUES}'))['certSecret']['name'])")"
if $K get secret "$CERT_SECRET" >/dev/null 2>&1; then
  echo "==> ${CERT_SECRET} present:"
  $K get secret "$CERT_SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d \
    | openssl x509 -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/    /'
else
  echo "==> WARNING: ${CERT_SECRET} does not exist yet."
  echo "    Run ./seed.sh before cutting infra/traefik over, or Traefik will"
  echo "    serve its self-signed default certificate on every host."
fi

if [[ "$RUN_NOW" == "true" ]]; then
  JOB="${RELEASE}-manual-$(date +%s)"
  echo "==> triggering ${JOB} from the CronJob"
  $K create job "$JOB" --from="cronjob/${RELEASE}"
  echo "==> waiting (ACME with DNS propagation is minutes, not seconds)"
  $K wait --for=condition=complete --timeout=900s "job/${JOB}" \
    || { echo "FAIL: job did not complete. Logs:"; $K logs "job/${JOB}" --all-containers --tail=100; exit 1; }
  echo "==> logs"
  $K logs "job/${JOB}" --all-containers --tail=60
  echo "==> certificate now published:"
  $K get secret "$CERT_SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d \
    | openssl x509 -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/    /'
fi
