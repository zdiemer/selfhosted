#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
readarray -t CONFIG < <(python3 - "$HERE/values.yaml" <<'PY'
import sys, yaml
v = yaml.safe_load(open(sys.argv[1]))
print(v['source']['commit'])
print(v['image']['repository'] + ':' + v['image']['tag'])
print(v['nas']['buildPath'])
PY
)
for exe in game.exe gamemd.exe; do
  [[ -s "${CONFIG[2]}/$exe" ]] || { echo "Missing retail input: ${CONFIG[2]}/$exe" >&2; exit 1; }
done
RETAIL_CONTEXT="$(mktemp -d /tmp/ra2-retail-build.XXXXXX)"
trap 'rm -rf "$RETAIL_CONTEXT"' EXIT
cp "${CONFIG[2]}/game.exe" "${CONFIG[2]}/gamemd.exe" "$RETAIL_CONTEXT/"
docker build --build-arg "SOURCE_COMMIT=${CONFIG[0]}" \
  --build-context "retail=$RETAIL_CONTEXT" -t "${CONFIG[1]}" "$HERE"
docker push "${CONFIG[1]}"
