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
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

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

echo "==> helm upgrade ${RELEASE} itzg/minecraft -n ${NAMESPACE} ${VALUE_ARGS[*]}"
helm upgrade "$RELEASE" itzg/minecraft -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for rollout"
$K rollout status "deployment/${RELEASE}-minecraft" --timeout=600s

echo "==> Tailing logs (Ctrl-C to exit; server keeps running)"
NEW_POD=$($K get pod -l "app=${RELEASE}-minecraft" -o jsonpath='{.items[0].metadata.name}')
exec $K logs -f "$NEW_POD" -c "$MC_CTR"
