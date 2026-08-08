#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

# Reclaim the rollback PVs left behind by the local-path -> TrueNAS migration.
#
# scripts/k3s/pvc-migrate.sh deliberately leaves each source PV Retained and
# reserved with a claimRef nothing will ever match, so a migration can be undone
# by rebinding it. Once the data is genuinely backed up somewhere else, those
# copies are just disk sitting on nodes that needed the space back.
#
# Reclaiming means TWO things, and doing only the first is the common mistake:
#   1. delete the PV object
#   2. delete the directory under /var/lib/rancher/k3s/storage on its node
# A Retained PV's data does not go away when the object does — that is the whole
# point of Retain — so step 1 alone frees nothing and leaves an orphan that
# cleanup.sh will report forever.
#
# SAFETY. Every PV is checked against the restic repository first: if k8up has no
# snapshot covering that claim, the PV is SKIPPED, because deleting it would
# leave that volume with no copy anywhere. --force overrides, and says so.
#
# Read-only by default.

APPLY=false
FORCE=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Delete the migration's rollback PVs and free the disk they hold, but only where
k8up has a snapshot of the same claim.

OPTIONS:
  --apply     Actually delete (default: report only)
  --force     Reclaim even without a backup covering the claim. Dangerous:
              that volume then exists in exactly one place.
  -h, --help  Show this help
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --apply) APPLY=true; shift ;;
        --force) FORCE=true; shift ;;
        -h|--help) usage ;;
        *) echo "Error: Unknown option: $1" >&2; usage ;;
    esac
done

require_tools kubectl python3

# ------------------------------------------------------------------------------
# What k8up has actually stored
# ------------------------------------------------------------------------------
# Snapshot paths look like /data/<pvc-name>, so the claim name is the basename.
# Matching on that rather than on the PV name because a restored volume is a
# different PV with the same claim.
echo "=== Backup coverage ==="
mapfile -t COVERED < <(kubectl get snapshots.k8up.io -A -o json 2>/dev/null | python3 -c "
import json, sys, os
try: d = json.load(sys.stdin)
except Exception: raise SystemExit
out = set()
for s in d.get('items', []):
    ns = s['metadata']['namespace']
    for p in ((s.get('spec') or {}).get('paths') or []):
        if p.startswith('/data/'):
            out.add(f'{ns}/{os.path.basename(p)}')
print('\n'.join(sorted(out)))
")
if [[ ${#COVERED[@]} -eq 0 ]]; then
    echo "  [WARN] k8up reports no snapshots at all."
else
    echo "  ${#COVERED[@]} claim(s) covered by a snapshot"
fi

covered() {
    local want="$1" c
    for c in ${COVERED[@]+"${COVERED[@]}"}; do
        [[ "$c" == "$want" ]] && return 0
    done
    return 1
}

# ------------------------------------------------------------------------------
# The rollback PVs
# ------------------------------------------------------------------------------
mapfile -t ROWS < <(kubectl get pv -o json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for p in d['items']:
    s = p['spec']
    c = s.get('claimRef') or {}
    name = c.get('name', '')
    if 'DO-NOT-BIND' not in name:
        continue
    claim = name.replace('-ROLLBACK-DO-NOT-BIND', '').replace('-ORPHAN-DO-NOT-BIND', '')
    node = ''
    for t in (s.get('nodeAffinity', {}).get('required', {}).get('nodeSelectorTerms') or []):
        for e in t.get('matchExpressions', []):
            node = ','.join(e.get('values', []))
    path = (s.get('local') or {}).get('path') or (s.get('hostPath') or {}).get('path') or ''
    print('\t'.join([p['metadata']['name'], c.get('namespace',''), claim, node, path,
                     s.get('persistentVolumeReclaimPolicy',''), p['status']['phase']]))
")

if [[ ${#ROWS[@]} -eq 0 ]]; then
    echo ""
    echo "No rollback PVs found. Nothing to do."
    exit 0
fi

echo ""
[[ "$APPLY" == "true" ]] && echo "=== RECLAIMING ===" || echo "=== AUDIT ONLY (use --apply) ==="
printf '  %-38s %-10s %-24s %s\n' CLAIM SIZE NODE STATUS
RECLAIM=(); SKIP=()
for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r pv ns claim node path policy phase <<< "$row"
    key="${ns}/${claim}"
    if covered "$key"; then
        state="backed up"
        RECLAIM+=("$row")
    elif [[ "$FORCE" == "true" ]]; then
        state="NO BACKUP (forced)"
        RECLAIM+=("$row")
    else
        state="NO BACKUP — skipping"
        SKIP+=("$row")
    fi
    printf '  %-38s %-10s %-24s %s\n' "$key" "$phase" "$node" "$state"
done

echo ""
echo "  ${#RECLAIM[@]} to reclaim, ${#SKIP[@]} skipped"

if [[ "$APPLY" != "true" ]]; then
    echo ""
    echo "Re-run with --apply to delete these PVs and free their directories."
    exit 0
fi

[[ ${#RECLAIM[@]} -eq 0 ]] && { echo "Nothing to reclaim."; exit 0; }

# ------------------------------------------------------------------------------
# Reclaim
# ------------------------------------------------------------------------------
echo ""
FREED=0
for row in "${RECLAIM[@]}"; do
    IFS=$'\t' read -r pv ns claim node path policy phase <<< "$row"
    echo "--- ${ns}/${claim} ---"

    # Refuse to touch anything still bound to a live claim. A rollback PV should
    # never be, but this is the check whose absence would be unforgivable.
    live="$(kubectl get pv "$pv" -o jsonpath='{.spec.claimRef.name}' 2>/dev/null || true)"
    if [[ "$live" != *"DO-NOT-BIND"* ]]; then
        echo "  [SKIP] claimRef is now '${live}' — not a rollback PV any more"
        continue
    fi

    size="$(run_on_node_sudo "$node" "du -sb '$path' 2>/dev/null | cut -f1" 2>/dev/null | tr -d '[:space:]' || echo 0)"

    if kubectl delete pv "$pv" --wait=true >/dev/null 2>&1; then
        echo "  deleted pv/${pv}"
    else
        echo "  [ERROR] could not delete pv/${pv}"; continue
    fi

    # The data outlives the object — that is what Retain means. Remove it too, or
    # the disk is never actually freed.
    if [[ -n "$node" && -n "$path" ]]; then
        if run_on_node_sudo "$node" "rm -rf '$path'" >/dev/null 2>&1; then
            echo "  removed ${node}:${path}"
            [[ "$size" =~ ^[0-9]+$ ]] && FREED=$((FREED + size))
        else
            echo "  [WARN] could not remove ${node}:${path} — orphan left behind"
        fi
    fi
done

echo ""
echo "=== Done ==="
echo "  freed approximately $(numfmt --to=iec-i --suffix=B "$FREED" 2>/dev/null || echo "${FREED}B")"
if [[ ${#SKIP[@]} -gt 0 ]]; then
    echo ""
    echo "  ${#SKIP[@]} PV(s) kept because nothing has backed them up:"
    for row in "${SKIP[@]}"; do
        IFS=$'\t' read -r pv ns claim node path policy phase <<< "$row"
        printf '    %s/%s\n' "$ns" "$claim"
    done
    echo "  Back those up, then re-run."
fi
echo ""
echo "  Node disk now:"
kubectl get nodes -o name 2>/dev/null | sed 's|node/||' | while read -r n; do
    kubectl get --raw "/api/v1/nodes/${n}/proxy/stats/summary" 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
fs=d['node']['fs']
print(f\"    {d['node']['nodeName']:24} {fs['availableBytes']/2**30:6.0f}G free of {fs['capacityBytes']/2**30:.0f}G\")
" 2>/dev/null || true
done
