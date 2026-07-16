#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

TARGET_ALL=false
TARGET_NODE=""
FORCE=false
SERVICE_ONLY=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Restart k3s cluster nodes.

OPTIONS:
  --node <name>    Restart a specific node
  --all            Rolling restart of all nodes (agents first)
  --force          Skip kubectl drain/uncordon
  --service-only   Restart the k3s service instead of rebooting
  -h, --help       Show this help message

EXAMPLES:
  $(basename "$0") --node mynode
  $(basename "$0") --all
  $(basename "$0") --node mynode --service-only
  $(basename "$0") --all --force
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --all)
            TARGET_ALL=true
            shift
            ;;
        --node)
            TARGET_NODE="$2"
            shift 2
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --service-only)
            SERVICE_ONLY=true
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

TARGET_NAMES=()

if [[ "$TARGET_ALL" == "true" ]]; then
    get_all_nodes
    sort_nodes_agents_first
    TARGET_NAMES=("${NODE_NAMES[@]}")
elif [[ -n "$TARGET_NODE" ]]; then
    get_all_nodes
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
    select_node
    TARGET_NAMES=("$SELECTED_NODE_NAME")
fi

for i in "${!TARGET_NAMES[@]}"; do
    hostname="${TARGET_NAMES[$i]}"
    echo "=== Restarting $hostname ==="

    if [[ "$FORCE" != "true" ]]; then
        if ! drain_node "$hostname"; then
            echo "[ERROR] Failed to drain $hostname, aborting." >&2
            exit 1
        fi
    fi

    if [[ "$SERVICE_ONLY" == "true" ]]; then
        service_name=$(get_k3s_service_name "$hostname")
        echo "Restarting $service_name service on $hostname..."
        run_on_node_sudo "$hostname" \
            "systemctl restart $service_name"
        sleep 10

        if ! run_on_node "$hostname" \
            "systemctl is-active $service_name" &>/dev/null; then
            echo "[ERROR] $service_name failed to start on $hostname" >&2
            exit 1
        fi

        echo -n "Waiting for $hostname to become ready"
        elapsed=0
        while [[ $elapsed -lt $NODE_READY_TIMEOUT ]]; do
            status=$(kubectl get node "$hostname" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' \
                2>/dev/null || echo "")
            if [[ "$status" == "True" ]]; then
                echo ""
                echo "[SUCCESS] $hostname is ready"
                break
            fi
            echo -n "."
            sleep 5
            elapsed=$((elapsed + 5))
        done

        if [[ "$status" != "True" ]]; then
            echo ""
            echo "[ERROR] $hostname did not become ready" \
                "within ${NODE_READY_TIMEOUT}s" >&2
            exit 1
        fi
    else
        case "$hostname" in
            zachd-ubuntu-laptop-4|zachd-ubuntu-laptop-5)
                echo "[WARN] $hostname is a Chromebook with the original" \
                    "firmware intact. After POST you must manually select the" \
                    "alternate bootloader (e.g. CTRL+L on the chainload screen)" \
                    "or it will not boot back into Linux. The script will hang" \
                    "in 'wait_for_node_online' until you do."
                ;;
        esac
        echo "Rebooting $hostname..."
        run_on_node_sudo "$hostname" "reboot" || true
        sleep 10

        if ! wait_for_node_online "$hostname"; then
            echo "[ERROR] $hostname did not come back after reboot" >&2
            exit 1
        fi
    fi

    if [[ "$FORCE" != "true" ]]; then
        uncordon_node "$hostname"
    fi

    echo "[SUCCESS] $hostname restarted"
    echo ""
done

echo "=== Restart Complete ==="
