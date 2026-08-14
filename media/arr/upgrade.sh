#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running arr release.
#
# Flow:
#   1. Resolve the PIA credentials from 1Password into memory
#   2. helm upgrade
#   3. Wait for all four rollouts
#   4. Print pods + the VPN egress IP (should be a PIA address, never the house)

set -euo pipefail

RELEASE="${RELEASE:-arr}"
NAMESPACE="${NAMESPACE:-media}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# The PIA credentials are resolved from 1Password into memory for the life of
# this run and never written to a disk. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

# gluetun with blank credentials retries forever while reporting Running, so an
# empty resolve has to stop the deploy rather than reach the chart.
sv_has || { echo "FAIL: no PIA credentials resolved from 1Password."
            echo "  check with:  ./scripts/secrets.sh check media/arr"; exit 1; }

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

for d in prowlarr sonarr radarr qbittorrent flaresolverr; do
  echo "==> Waiting for ${RELEASE}-${d} rollout"
  $K rollout status "deployment/${RELEASE}-${d}" --timeout=300s
done

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"

echo "==> VPN egress IP (must be PIA, not the house)"
# Asked of the qbittorrent container, not gluetun: it is the one whose traffic
# the tunnel exists to hide, so it is the honest test of the shared netns. (It
# is also the one that works — gluetun's busybox wget cannot do HTTPS through
# gluetun's own DNS server and exits 4 no matter the tunnel state, which made
# this check silently useless.)
$K exec "deploy/${RELEASE}-qbittorrent" -c qbittorrent -- curl -s --max-time 15 https://ipinfo.io/ip || true
echo
