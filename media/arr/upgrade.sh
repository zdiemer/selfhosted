#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running arr release.
#
# Flow:
#   1. Materialize values.local.yaml from 1Password if missing
#   2. helm upgrade
#   3. Wait for all four rollouts
#   4. Print pods + the VPN egress IP (should be a PIA address, never the house)

set -euo pipefail

RELEASE="${RELEASE:-arr}"
NAMESPACE="${NAMESPACE:-media}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"

# Materialize values.local.yaml from 1Password when it's missing and a template
# exists. Convenience only: values.local.yaml is still the contract, so this
# no-ops without `op` — e.g. in the claude-workspace pod, which is fed by
# `scripts/secrets.sh publish` instead. See values.local.tpl.yaml.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

[[ -f "$LOCAL_VALUES" ]] || { echo "FAIL: ${LOCAL_VALUES} missing (PIA credentials). See values.local.yaml.example"; exit 1; }

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f "$LOCAL_VALUES" --cleanup-on-fail

for d in prowlarr sonarr radarr qbittorrent; do
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
