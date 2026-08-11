#!/usr/bin/env bash
# Say what draining a node will actually cost, before draining it.
#
# WHY
# `kubectl drain` reports what it deletes, not what that means. The questions
# worth asking before a maintenance window — which services go dark, what
# cannot move at all, what will sit waiting on a volume handoff — are not in
# its output. Most of this cluster is single-replica by necessity (single-writer
# RWO volumes), so "the pod gets recreated elsewhere" is both the normal case
# and the outage, and nothing distinguishes them at drain time.
#
# It is also the other half of a deliberate decision. Singletons here get no
# PodDisruptionBudget, because a minAvailable: 1 budget against one replica
# blocks a drain permanently rather than failing — see "Availability
# conventions" in the root README. This is what covers them instead: it reports
# the gap rather than pretending to prevent it.
#
# Read-only. It never cordons, evicts or patches anything.
#
# EXIT STATUS
#   0  nothing on the node gaps
#   2  something will gap while it moves (expected for most nodes here)
#   3  something will not move at all, or a PDB will block the drain
#
# so it can gate an unattended drain:
#
#     ./drain-preflight.sh --node zachd-ubuntu-2 || exit 1

set -uo pipefail

source "$(dirname "$0")/_common.sh"

TARGET_ALL=false
TARGET_NODE=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Report what a node drain will cost, without draining anything.

OPTIONS:
  --node <name>    Check a specific node
  --all            Check every node
  -h, --help       Show this help message

EXIT STATUS:
  0  nothing gaps
  2  something gaps while it moves
  3  something cannot move, or a PDB blocks the drain

EXAMPLES:
  $(basename "$0") --node zachd-ubuntu-2
  $(basename "$0") --all
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --all)  TARGET_ALL=true; shift ;;
        --node) TARGET_NODE="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Error: Unknown option: $1" >&2; usage ;;
    esac
done

require_tools kubectl python3

TARGETS=()
if [[ "$TARGET_ALL" == "true" ]]; then
    get_all_nodes
    TARGETS=("${NODE_NAMES[@]}")
elif [[ -n "$TARGET_NODE" ]]; then
    TARGETS=("$TARGET_NODE")
else
    echo "Error: specify --node <name> or --all." >&2
    usage
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"

# One API round trip for everything the analysis needs. `kubectl get a,b,c -o json`
# returns a single List, which is what drain_preflight.py expects on stdin.
kubectl get nodes,pods,poddisruptionbudgets,persistentvolumeclaims,deployments,statefulsets \
        --all-namespaces -o json \
    | python3 "${HERE}/lib/drain_preflight.py" "${TARGETS[@]}"
