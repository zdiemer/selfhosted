#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

# Flip every local-path PV to persistentVolumeReclaimPolicy=Retain.
#
# k3s local-path defaults to Delete. That means deleting a PVC — by hand, by a
# `helm upgrade` that renames a claim, by an existingClaim switch that lets helm
# prune the old one — immediately destroys the data on disk with no undo. There
# is currently no backup of any of it.
#
# Retain converts every one of those from "data is gone" into "PV is Released and
# the directory is still there". It is the single cheapest safety net available
# and it is a prerequisite for scripts/k3s/pvc-migrate.sh, which refuses to run
# against a Delete-policy source.
#
# This is purely protective: it can only prevent deletion, never cause it. The
# cost of leaving it on afterwards is that deleted PVCs leave orphaned
# directories behind — scripts/k3s/cleanup.sh already reports those.
#
# Read-only by default. Nothing changes without --apply.

APPLY=false
STORAGE_CLASS="local-path"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Set persistentVolumeReclaimPolicy=Retain on PVs so that deleting a PVC cannot
destroy the underlying data. Reports by default; use --apply to change anything.

OPTIONS:
  --apply             Actually patch the PVs (default: report only)
  --storage-class <n> Which class to guard (default: ${STORAGE_CLASS})
  --all               Guard every PV regardless of storage class
  -h, --help          Show this help message

EXAMPLES:
  $(basename "$0")                    # what would change
  $(basename "$0") --apply            # guard all local-path PVs
  $(basename "$0") --all --apply      # guard everything, including truenas-*
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --apply)
            APPLY=true
            shift
            ;;
        --storage-class)
            STORAGE_CLASS="$2"
            shift 2
            ;;
        --all)
            STORAGE_CLASS=""
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            usage
            ;;
    esac
done

require_tools kubectl

if [[ -n "$STORAGE_CLASS" ]]; then
    echo "=== PVs on storage class '${STORAGE_CLASS}' ==="
    SELECT=".spec.storageClassName == \"${STORAGE_CLASS}\""
else
    echo "=== All PVs ==="
    SELECT="true"
fi

if [[ "$APPLY" != "true" ]]; then
    echo "(AUDIT ONLY — nothing will be changed, use --apply)"
fi
echo ""

# name, policy, claim, capacity — one line per PV.
mapfile -t ROWS < <(kubectl get pv -o json | python3 -c "
import json, sys
sel = ${SELECT@Q}
d = json.load(sys.stdin)
for p in d['items']:
    s = p['spec']
    if sel != 'true' and s.get('storageClassName') != ${STORAGE_CLASS@Q}:
        continue
    c = s.get('claimRef', {})
    print('\t'.join([
        p['metadata']['name'],
        s.get('persistentVolumeReclaimPolicy', '?'),
        f\"{c.get('namespace','-')}/{c.get('name','-')}\",
        s.get('capacity', {}).get('storage', '?'),
        p.get('status', {}).get('phase', '?'),
    ]))
")

if [[ ${#ROWS[@]} -eq 0 ]]; then
    echo "No matching PVs found."
    exit 0
fi

printf '%-42s %-8s %-38s %-8s %s\n' NAME POLICY CLAIM SIZE PHASE
NEEDS_PATCH=()
for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r name policy claim size phase <<< "$row"
    printf '%-42s %-8s %-38s %-8s %s\n' "$name" "$policy" "$claim" "$size" "$phase"
    [[ "$policy" != "Retain" ]] && NEEDS_PATCH+=("$name")
done
echo ""

if [[ ${#NEEDS_PATCH[@]} -eq 0 ]]; then
    echo "[OK] Every matching PV is already Retain. Nothing to do."
    exit 0
fi

echo "${#NEEDS_PATCH[@]} PV(s) would lose their data if the PVC were deleted:"
printf '  %s\n' "${NEEDS_PATCH[@]}"
echo ""

if [[ "$APPLY" != "true" ]]; then
    echo "Re-run with --apply to set them to Retain."
    exit 1
fi

echo "=== Patching to Retain ==="
FAILED=()
for pv in "${NEEDS_PATCH[@]}"; do
    if kubectl patch pv "$pv" --type=merge \
        -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}' >/dev/null 2>&1; then
        echo "  [OK]   $pv"
    else
        echo "  [FAIL] $pv"
        FAILED+=("$pv")
    fi
done
echo ""

# Read back rather than trusting the patch calls — this is the guarantee the
# rest of the migration is built on, so verify it rather than assume it.
echo "=== Verify ==="
STILL_BAD=$(kubectl get pv -o json | python3 -c "
import json, sys
d = json.load(sys.stdin)
bad = [p['metadata']['name'] for p in d['items']
       if (${SELECT@Q} == 'true' or p['spec'].get('storageClassName') == ${STORAGE_CLASS@Q})
       and p['spec'].get('persistentVolumeReclaimPolicy') != 'Retain']
print('\n'.join(bad))
")

if [[ -n "$STILL_BAD" ]]; then
    echo "[ERROR] Still not Retain:"
    echo "$STILL_BAD" | sed 's/^/  /'
    exit 1
fi

echo "[OK] Every matching PV is now Retain."
echo ""
echo "Deleting a PVC now leaves its PV Released with the data intact, rather"
echo "than destroying it. scripts/k3s/pvc-migrate.sh depends on this."
[[ ${#FAILED[@]} -gt 0 ]] && exit 1
exit 0
