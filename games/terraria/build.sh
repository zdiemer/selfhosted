#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG=$(awk -F'"' '/^  tag:/{print $2; exit}' "$HERE/values.yaml")
REGISTRY=registry.zachd.duckdns.org/zdiemer
for target in server bridge; do
  name=terraria-server
  [[ $target != bridge ]] || name=terraria-claude
  docker build --target "$target" -t "$REGISTRY/$name:$TAG" "$HERE"
  if [[ $target == server ]]; then
    docker run --rm --entrypoint cat "$REGISTRY/$name:$TAG" /opt/mods/lock.json | \
      python3 -c 'import json,sys; actual=json.load(sys.stdin); expected=json.load(open(sys.argv[1])); sys.exit(0 if actual==expected else "Workshop content changed. Review /opt/mods/lock.json in the built image, update mods.lock.json deliberately, and bump the image tags before deploying.")' "$HERE/mods.lock.json"
  fi
  docker push "$REGISTRY/$name:$TAG"
done
