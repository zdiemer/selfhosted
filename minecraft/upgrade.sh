#!/usr/bin/env bash
# Safely apply the current values.yaml to the running server.
#
# Flow:
#   1. RCON save-all flush (world hits disk)
#   2. Trigger an immediate backup via the mc-backup sidecar
#   3. helm upgrade (Recreate strategy: old pod terminates, new pod starts)
#   4. Wait for Ready, tail logs
#
# Plugin versions are resolved by the itzg image on every pod start from the
# MODRINTH_PROJECTS / PLUGINS env vars in values.yaml. To bump a plugin,
# either pin `plugin:<slug>:<versionId>` in values.yaml or delete the pod
# (latest compatible version gets pulled on next boot).

set -euo pipefail

RELEASE="${RELEASE:-mc}"
NAMESPACE="${NAMESPACE:-minecraft}"
# Pin the chart — this deploy interrupts a live world; a surprise chart bump
# should never ride along with a routine values change. Bump deliberately.
# renovate: datasource=helm depName=minecraft registryUrl=https://itzg.github.io/minecraft-server-charts/
CHART_VERSION="${CHART_VERSION:-5.1.3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# The secrets are resolved from 1Password into memory for the life of this run
# and never written to a disk. Each helm call spells out its own `-f <(sv_fd)`
# rather than carrying it in VALUE_ARGS: that fd is a pipe, readable once, so a
# shared one would hand the second reader an empty values file. See
# scripts/lib/secret-values.sh.
. "${HERE}/../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> Finding current pod for ${RELEASE}"
POD=$($K get pod -l "app=${RELEASE}-minecraft" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
: "${POD:?no running pod found — is the release installed?}"
echo "    pod=${POD}"

echo "==> Discovering container names"
MC_CTR=$($K get pod "$POD" \
  -o jsonpath='{.spec.containers[?(@.name!="mc-backup")].name}' | awk '{print $1}')
BACKUP_CTR=$($K get pod "$POD" \
  -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}' | grep -E 'backup' || true)
: "${MC_CTR:?could not find minecraft container}"
echo "    mc=${MC_CTR}  backup=${BACKUP_CTR:-<none>}"

echo "==> Flushing world to disk via RCON"
$K exec "$POD" -c "$MC_CTR" -- rcon-cli save-all flush

if [[ -n "${BACKUP_CTR:-}" ]]; then
  echo "==> Triggering manual backup via sidecar"
  $K exec "$POD" -c "$BACKUP_CTR" -- sh -c 'pkill -USR1 -f backup-loop.sh' \
    || echo "    (manual trigger unsupported on this image — hourly cron still active)"
  sleep 15
fi

echo "==> helm upgrade ${RELEASE} itzg/minecraft@${CHART_VERSION} -n ${NAMESPACE} ${VALUE_ARGS[*]}"
helm upgrade "$RELEASE" itzg/minecraft --version "$CHART_VERSION" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}-minecraft" --timeout=600s

echo "==> Tailing logs (Ctrl-C to exit; server keeps running)"
NEW_POD=$($K get pod -l "app=${RELEASE}-minecraft" -o jsonpath='{.items[0].metadata.name}')
exec $K logs -f "$NEW_POD" -c "$MC_CTR"
