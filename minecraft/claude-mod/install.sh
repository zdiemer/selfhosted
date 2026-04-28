#!/usr/bin/env bash
# Build the claude-mod JAR and sideload it into the running Minecraft pod.
#
# No local Java required — the build runs in a one-shot gradle:8-jdk17
# container. Output is `build/libs/claude-mod-<version>.jar`.
#
# After kubectl cp the JAR into /data/mods, the script restarts the
# Minecraft pod so Fabric loads the new mod. **THIS DROPS ALL PLAYERS.**
# Run when no one's online (or schedule it).
#
# To upgrade the mod:
#   - bump `mod_version` in gradle.properties
#   - re-run this script (overwrites the existing claude-mod.jar in /data/mods)
#
# Env knobs:
#   NAMESPACE  — k8s namespace of the Minecraft pod (default: minecraft)
#   SKIP_BUILD — if set, skip gradle build and reuse the existing jar
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NS="${NAMESPACE:-minecraft}"

command -v docker  >/dev/null || { echo "docker required";  exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

if [[ -z "${SKIP_BUILD:-}" ]]; then
  echo "==> building JAR via dockerized gradle (first run downloads ~200MB)"
  # --user matches host UID so build/ output is owned by us, not root.
  # gradle home is mounted to a host cache so subsequent builds are fast.
  mkdir -p "${HOME}/.gradle-claude-mod"
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$HERE:/work" \
    -v "${HOME}/.gradle-claude-mod:/home/gradle/.gradle" \
    -w /work \
    -e GRADLE_USER_HOME=/home/gradle/.gradle \
    gradle:8.6-jdk17 \
    gradle --no-daemon build
fi

JAR="$(ls -1t "$HERE"/build/libs/claude-mod-*.jar 2>/dev/null \
       | grep -v '\-sources\.jar$' | head -1)"
if [[ -z "$JAR" ]]; then
  echo "no JAR found in build/libs/ — build failed?"; exit 1
fi
echo "==> built $(basename "$JAR")"

POD="$(kubectl -n "$NS" get pod -l app=mc-minecraft -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$POD" ]]; then
  echo "no Minecraft pod found in namespace $NS"; exit 1
fi

echo "==> copying $(basename "$JAR") -> $POD:/data/mods/claude-mod.jar"
kubectl -n "$NS" cp "$JAR" "${POD}:/data/mods/claude-mod.jar" -c mc-minecraft

echo "==> flushing world"
kubectl -n "$NS" exec "$POD" -c mc-minecraft -- rcon-cli save-all flush >/dev/null

echo "==> restarting Minecraft pod (players will be disconnected)"
kubectl -n "$NS" delete pod "$POD" --wait=false
kubectl -n "$NS" rollout status deployment/mc-minecraft --timeout=600s

echo "==> done. Tail logs to confirm Fabric picked up the mod:"
echo "    kubectl -n $NS logs -f deploy/mc-minecraft -c mc-minecraft | grep -E '(claudemod|Loading|ClaudeRequest)'"
