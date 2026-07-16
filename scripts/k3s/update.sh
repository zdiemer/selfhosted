#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

AUTO_REBOOT=false
SKIP_DRAIN=false
TARGET_NODE=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Rolling update/upgrade of packages on k3s cluster nodes.
By default, updates all nodes sequentially (agents first).

OPTIONS:
  --node <name>    Update a specific node only
  --reboot         Auto-reboot if the node requires it
  --skip-drain     Skip kubectl drain/uncordon
  -h, --help       Show this help message

EXAMPLES:
  $(basename "$0")
  $(basename "$0") --reboot
  $(basename "$0") --node mynode --reboot
  $(basename "$0") --skip-drain
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --node)
            TARGET_NODE="$2"
            shift 2
            ;;
        --reboot)
            AUTO_REBOOT=true
            shift
            ;;
        --skip-drain)
            SKIP_DRAIN=true
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

require_tools kubectl tailscale

get_all_nodes

TARGET_NAMES=()

if [[ -n "$TARGET_NODE" ]]; then
    for name in "${NODE_NAMES[@]}"; do
        if [[ "$name" == "$TARGET_NODE" ]]; then
            TARGET_NAMES=("$name")
            break
        fi
    done
    if [[ ${#TARGET_NAMES[@]} -eq 0 ]]; then
        echo "Error: Node '$TARGET_NODE' not found." >&2
        exit 1
    fi
else
    sort_nodes_agents_first
    TARGET_NAMES=("${NODE_NAMES[@]}")
fi

for i in "${!TARGET_NAMES[@]}"; do
    hostname="${TARGET_NAMES[$i]}"
    echo "=== Updating $hostname ==="

    if [[ "$SKIP_DRAIN" != "true" ]]; then
        if ! drain_node "$hostname"; then
            echo "[ERROR] Failed to drain $hostname, aborting." >&2
            exit 1
        fi
    fi

    cleanup() {
        if [[ "$SKIP_DRAIN" != "true" ]]; then
            echo "Ensuring $hostname is uncordoned..."
            uncordon_node "$hostname" 2>/dev/null || true
        fi
    }
    trap cleanup EXIT

    echo "--- Running apt upgrade ---"
    if ! run_on_node_sudo "$hostname" \
        "apt-get update && apt-get upgrade -y"; then
        echo "[ERROR] Package upgrade failed on $hostname" >&2
        exit 1
    fi
    echo "[SUCCESS] Packages updated on $hostname"

    reboot_needed=$(check_reboot_required "$hostname")
    if [[ "$reboot_needed" == "yes" ]]; then
        if [[ "$AUTO_REBOOT" == "true" ]]; then
            echo "[WARNING] Reboot required, rebooting $hostname..."
            run_on_node_sudo "$hostname" "reboot" || true
            sleep 10
            if ! wait_for_node_online "$hostname"; then
                echo "[ERROR] $hostname did not come back after reboot" >&2
                exit 1
            fi
        else
            echo "[WARNING] Reboot required on $hostname." \
                "Use --reboot to auto-reboot."
        fi
    fi

    if [[ "$SKIP_DRAIN" != "true" ]]; then
        uncordon_node "$hostname"
    fi

    trap - EXIT
    echo "[SUCCESS] $hostname updated and ready"
    echo ""
done

echo "=== Update Complete ==="
