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
# MODRINTH_PROJECTS / PLUGINS env vars in values.yaml. To bump a plugin, either
# edit its `plugin:<slug>:<versionId>` pin in values.yaml or just delete the
# pod (latest compatible version gets pulled on next boot).

set -euo pipefail

RELEASE="${RELEASE:-mc}"
POD="${RELEASE}-minecraft-0"
VALUES="$(cd "$(dirname "$0")" && pwd)/values.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> Discovering container names in ${POD}"
MC_CTR=$(kubectl get pod "$POD" \
  -o jsonpath='{.spec.containers[?(@.name!="mc-backup")].name}' | awk '{print $1}')
BACKUP_CTR=$(kubectl get pod "$POD" \
  -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}' | grep -E 'backup' || true)
: "${MC_CTR:?could not find minecraft container}"
echo "    mc=${MC_CTR}  backup=${BACKUP_CTR:-<none>}"

echo "==> Flushing world to disk via RCON"
kubectl exec "$POD" -c "$MC_CTR" -- rcon-cli save-all flush

if [[ -n "${BACKUP_CTR:-}" ]]; then
  echo "==> Triggering manual backup via sidecar"
  # itzg/mc-backup's loop uses an interruptible sleep; USR1 skips the wait.
  # Older image tags may not support this — hourly cron is the fallback.
  kubectl exec "$POD" -c "$BACKUP_CTR" -- sh -c 'pkill -USR1 -f backup-loop.sh' \
    || echo "    (manual trigger unsupported on this image — hourly cron still active)"
  sleep 15
fi

echo "==> helm upgrade ${RELEASE} itzg/minecraft -f values.yaml"
helm upgrade "$RELEASE" itzg/minecraft -f "$VALUES"

echo "==> Waiting for old pod to terminate"
kubectl wait --for=delete "pod/${POD}" --timeout=300s || true

echo "==> Waiting for new pod to become Ready (plugin downloads take a minute)"
kubectl wait --for=condition=Ready "pod/${POD}" --timeout=600s

echo "==> Tailing logs (Ctrl-C to exit; server keeps running)"
exec kubectl logs -f "$POD" -c "$MC_CTR"
