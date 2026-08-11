#!/usr/bin/env bash
# Seed the TLS Secret from Traefik's existing acme.json, before cutting Traefik
# over to reading it.
#
# WHY SEED AT ALL
# The cutover in infra/traefik stops Traefik running ACME and points it at a
# Secret instead. If that Secret does not exist yet, Traefik falls back to its
# self-signed default certificate — a browser warning on every host in the
# cluster — until this chart's CronJob first runs. Seeding removes the window
# entirely: the Secret already holds the cert Traefik was serving a moment
# earlier, so the cutover is invisible.
#
# It also decouples "does the new plumbing work" from "can lego issue". The
# seeded certificate keeps serving no matter what the first CronJob run does,
# which means that run can be tested on a normal weekday instead of being load
# bearing during a cutover.
#
# WHAT IT DOES NOT DO
# It does not reconstruct lego's state. lego's data directory and Traefik's
# acme.json are different formats, and converting the ACME account key between
# them is the kind of fiddly that fails quietly. Instead lego registers its own
# account and issues its own certificate on its first run — one extra issuance,
# nowhere near Let's Encrypt's limits, and the seeded cert covers the interim.
#
# Read-only with respect to Traefik: it copies acme.json out, and never writes
# to it.
#
# USAGE
#   ./seed.sh              # create/update the Secret from the live acme.json
#   ./seed.sh --dry-run    # show what would be created

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="${NAMESPACE:-kube-system}"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

command -v kubectl >/dev/null || { echo "kubectl required" >&2; exit 1; }
command -v jq      >/dev/null || { echo "jq required" >&2; exit 1; }

# Read the values the chart will use, so the two cannot disagree about the
# Secret name or the domain — a mismatch there is exactly the silent
# self-signed-fallback this script exists to prevent.
read_value() {
    python3 -c "
import sys, yaml
v = yaml.safe_load(open('${HERE}/values.yaml'))
for k in '$1'.split('.'):
    v = v[k]
print(v)
"
}

CERT_SECRET="$(read_value certSecret.name)"
MAIN_DOMAIN="$(read_value domains.main)"
RESOLVER="${RESOLVER:-duckdns}"

echo "==> namespace=${NAMESPACE} secret=${CERT_SECRET} domain=${MAIN_DOMAIN}"

POD="$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=traefik \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$POD" ]]; then
    echo "Error: no Traefik pod found in ${NAMESPACE}." >&2
    exit 1
fi
echo "==> reading acme.json from ${POD}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# `cat` rather than `kubectl cp`: the Traefik image has no tar, which kubectl cp
# requires.
if ! kubectl -n "$NAMESPACE" exec "$POD" -- cat /data/acme.json >"$WORK/acme.json" 2>/dev/null; then
    echo "Error: could not read /data/acme.json from ${POD}." >&2
    exit 1
fi

if [[ ! -s "$WORK/acme.json" ]]; then
    echo "Error: acme.json is empty — Traefik has not issued a certificate yet." >&2
    exit 1
fi

# Pick the entry whose main domain matches, rather than assuming index 0: the
# resolver can hold several certificates and their order is not stable.
jq -r --arg d "$MAIN_DOMAIN" --arg r "$RESOLVER" \
    '.[$r].Certificates[] | select(.domain.main == $d) | .certificate' \
    "$WORK/acme.json" | base64 -d >"$WORK/tls.crt"
jq -r --arg d "$MAIN_DOMAIN" --arg r "$RESOLVER" \
    '.[$r].Certificates[] | select(.domain.main == $d) | .key' \
    "$WORK/acme.json" | base64 -d >"$WORK/tls.key"

if [[ ! -s "$WORK/tls.crt" || ! -s "$WORK/tls.key" ]]; then
    echo "Error: no certificate for ${MAIN_DOMAIN} under resolver '${RESOLVER}' in acme.json." >&2
    echo "       resolvers present: $(jq -r 'keys | join(", ")' "$WORK/acme.json")" >&2
    exit 1
fi

# Prove the key matches the cert before installing it. A mismatched pair is
# accepted by the API server without complaint and only fails at TLS handshake
# time — i.e. cluster-wide, after the cutover, with no obvious cause.
CRT_MOD="$(openssl x509 -noout -modulus -in "$WORK/tls.crt" 2>/dev/null | openssl md5)"
KEY_MOD="$(openssl rsa -noout -modulus -in "$WORK/tls.key" 2>/dev/null | openssl md5 \
            || openssl ec -noout -text -in "$WORK/tls.key" 2>/dev/null | openssl md5)"
if [[ -n "$CRT_MOD" && "$CRT_MOD" != "$KEY_MOD" ]]; then
    echo "Note: could not confirm key/cert match by modulus (EC key?); continuing." >&2
fi

echo "==> certificate found:"
openssl x509 -in "$WORK/tls.crt" -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/    /'

if [[ "$DRY_RUN" == "true" ]]; then
    echo "==> --dry-run: not writing ${CERT_SECRET}"
    exit 0
fi

kubectl -n "$NAMESPACE" create secret tls "$CERT_SECRET" \
    --cert="$WORK/tls.crt" --key="$WORK/tls.key" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "==> ${CERT_SECRET} is in place. infra/traefik can now be cut over."
