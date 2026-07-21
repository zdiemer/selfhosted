#!/usr/bin/env bash
# Copy every gitignored values.local.yaml from this checkout into the
# claude-workspace pod's repo clone. Without this, deploying a chart from the
# pod either fails on `required` values or — worse — silently renders secret
# values empty (helm has no idea a second -f file was supposed to exist).
#
# One-way, laptop → pod. Re-run after adding or changing any values.local.yaml.
#
# Usage: scripts/sync-local-values.sh [--dry-run]

set -euo pipefail

NAMESPACE="${NAMESPACE:-claude}"
DEPLOY="${DEPLOY:-claude-workspace}"
POD_REPO="${POD_REPO:-/home/node/code/selfhosted}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

cd "$ROOT"
mapfile -t FILES < <(find . -name values.local.yaml -not -path './.git/*' | sort)
[[ ${#FILES[@]} -gt 0 ]] || { echo "no values.local.yaml files found — nothing to sync"; exit 0; }

echo "==> Files to sync (${#FILES[@]}):"
printf '    %s\n' "${FILES[@]}"

if [[ "${1:-}" == "--dry-run" ]]; then
  echo "==> Dry run, stopping here."
  exit 0
fi

# Fail with a real message if the pod hasn't cloned the repo yet — tar's own
# error ("cannot chdir") is easy to misread as a sync-side problem.
kubectl -n "$NAMESPACE" exec "deploy/${DEPLOY}" -c term -- test -d "$POD_REPO" || {
  echo "FAIL: ${POD_REPO} does not exist in the pod. Clone the repo there first."; exit 1; }

echo "==> Syncing into ${DEPLOY} (ns ${NAMESPACE}) at ${POD_REPO}"
tar czf - "${FILES[@]}" | \
  kubectl -n "$NAMESPACE" exec -i "deploy/${DEPLOY}" -c term -- \
  tar xzf - -C "$POD_REPO"

echo "==> Done."
