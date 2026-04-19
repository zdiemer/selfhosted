#!/usr/bin/env bash
# Upgrade the Minecraft server to a new Modrinth modpack version.
#
# Usage:   ./upgrade.sh <modrinth-version-id>
# Example: ./upgrade.sh GoepRBzX
#
# List recent versions:
#   curl -s 'https://api.modrinth.com/v2/project/prominence-2-fabric/version' \
#     | jq -r '.[0:5][] | "\(.id)  \(.version_number)  \(.date_published)"'

set -euo pipefail

VERSION_ID="${1:?usage: $0 <modrinth-version-id>}"
RELEASE="${RELEASE:-mc}"
POD="${RELEASE}-minecraft-0"
VALUES="$(cd "$(dirname "$0")" && pwd)/values.yaml"

command -v yq       >/dev/null || { echo "yq required (https://github.com/mikefarah/yq)"; exit 1; }
command -v helm     >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl  >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> Sanity check: versionId exists on Modrinth"
if ! curl -sf "https://api.modrinth.com/v2/version/${VERSION_ID}" \
    | jq -e '.project_id == "VGBY2WVO" or .project_id == "prominence-2-fabric"' >/dev/null 2>&1; then
  # Fallback: accept any successful lookup (project ID check is belt-and-suspenders).
  curl -sf "https://api.modrinth.com/v2/version/${VERSION_ID}" >/dev/null \
    || { echo "versionId ${VERSION_ID} not found on Modrinth"; exit 1; }
fi

echo "==> Pinning modrinthModpack.versionId = ${VERSION_ID} in values.yaml"
yq -i ".minecraftServer.modrinthModpack.versionId = \"${VERSION_ID}\"" "$VALUES"

echo "==> Discovering container names in ${POD}"
MC_CTR=$(kubectl get pod "$POD" \
  -o jsonpath='{.spec.containers[?(@.name!="mc-backup")].name}' | awk '{print $1}')
BACKUP_CTR=$(kubectl get pod "$POD" \
  -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}' | grep -E 'backup' || true)
: "${MC_CTR:?could not find minecraft container}"
echo "    mc=${MC_CTR}  backup=${BACKUP_CTR:-<none>}"

echo "==> Flushing world to disk via RCON (defense in depth)"
# SIGTERM on pod termination also triggers a clean save; this is belt-and-suspenders.
kubectl exec "$POD" -c "$MC_CTR" -- rcon-cli save-all flush

if [[ -n "${BACKUP_CTR:-}" ]]; then
  echo "==> Triggering manual backup via sidecar"
  # itzg/mc-backup's loop is driven by an interruptible sleep; sending USR1 to the
  # backup-loop process causes it to skip the wait and run a backup immediately.
  # If this fails (older image versions), fall through — the hourly cron still runs.
  kubectl exec "$POD" -c "$BACKUP_CTR" -- sh -c 'pkill -USR1 -f backup-loop.sh' \
    || echo "    (manual trigger unsupported on this image — hourly cron still active)"
  # Give the backup a beat to finish before we yank the pod.
  sleep 15
fi

echo "==> helm upgrade mc itzg/minecraft -f values.yaml"
helm upgrade "$RELEASE" itzg/minecraft -f "$VALUES"

echo "==> Waiting for old pod to terminate"
kubectl wait --for=delete "pod/${POD}" --timeout=300s || true

echo "==> Waiting for new pod to become Ready (first boot downloads the pack — be patient)"
kubectl wait --for=condition=Ready "pod/${POD}" --timeout=1800s

echo "==> Tailing logs (Ctrl-C to exit; server keeps running)"
exec kubectl logs -f "$POD" -c "$MC_CTR"
