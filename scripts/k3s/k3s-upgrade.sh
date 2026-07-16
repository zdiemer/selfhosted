#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

TARGET_NODE=""
TARGET_VERSION=""
SKIP_DRAIN=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Rolling upgrade of k3s across cluster nodes.
Upgrades agents first, then server. Re-runs the k3s installer
with the specified version on each node.

OPTIONS:
  --version <ver>  Target k3s version (e.g. v1.31.4+k3s1)
                   If omitted, upgrades to the latest stable
  --node <name>    Upgrade a specific node only
  --skip-drain     Skip kubectl drain/uncordon
  -h, --help       Show this help message

EXAMPLES:
  $(basename "$0")
  $(basename "$0") --version v1.31.4+k3s1
  $(basename "$0") --node mynode
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --version)
            TARGET_VERSION="$2"
            shift 2
            ;;
        --node)
            TARGET_NODE="$2"
            shift 2
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

require_tools kubectl tailscale curl

get_all_nodes

echo "=== Current k3s Versions ==="
kubectl get nodes \
    -o custom-columns=\
'NAME:.metadata.name,ROLE:.metadata.labels.node-role\.kubernetes\.io/control-plane,VERSION:.status.nodeInfo.kubeletVersion' \
    --no-headers
echo ""

if [[ -z "$TARGET_VERSION" ]]; then
    echo "Fetching latest stable k3s version..."
    TARGET_VERSION=$(curl -sL \
        "https://update.k3s.io/v1-release/channels/stable" \
        -o /dev/null -w '%{redirect_url}' \
        | grep -oP 'v[^/]+$')
    if [[ -z "$TARGET_VERSION" ]]; then
        echo "Error: Could not determine latest k3s version." >&2
        exit 1
    fi
fi
echo "Target version: $TARGET_VERSION"
echo ""

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

INSTALL_ENV="INSTALL_K3S_VERSION=$TARGET_VERSION"

for hostname in "${TARGET_NAMES[@]}"; do
    role=$(get_node_role "$hostname")
    service_name=$(get_k3s_service_name "$hostname")

    current_version=$(kubectl get node "$hostname" \
        -o jsonpath='{.status.nodeInfo.kubeletVersion}' \
        2>/dev/null || echo "unknown")

    if [[ "$current_version" == "$TARGET_VERSION" ]]; then
        echo "=== $hostname ($role) already at $TARGET_VERSION, skipping ==="
        echo ""
        continue
    fi

    echo "=== Upgrading $hostname ($role): $current_version -> $TARGET_VERSION ==="

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

    echo "--- Stopping $service_name ---"
    run_on_node_sudo "$hostname" \
        "systemctl stop $service_name"

    echo "--- Installing k3s $TARGET_VERSION ---"
    if [[ "$role" == "server" ]]; then
        run_on_node_sudo "$hostname" \
            "curl -sfL https://get.k3s.io | $INSTALL_ENV sh -s -"
    else
        run_on_node_sudo "$hostname" \
            "curl -sfL https://get.k3s.io | $INSTALL_ENV INSTALL_K3S_SKIP_START=true sh -s - agent"
        echo "--- Starting $service_name ---"
        run_on_node_sudo "$hostname" \
            "systemctl start $service_name"
    fi

    echo "--- Waiting for $hostname to become ready ---"
    if ! wait_for_node_online "$hostname"; then
        echo "[ERROR] $hostname did not become ready after upgrade" >&2
        exit 1
    fi

    new_version=$(kubectl get node "$hostname" \
        -o jsonpath='{.status.nodeInfo.kubeletVersion}' \
        2>/dev/null || echo "unknown")
    echo "[SUCCESS] $hostname upgraded to $new_version"

    if [[ "$SKIP_DRAIN" != "true" ]]; then
        uncordon_node "$hostname"
    fi

    trap - EXIT
    echo ""
done

echo "=== Upgrade Complete ==="
echo ""
kubectl get nodes \
    -o custom-columns=\
'NAME:.metadata.name,ROLE:.metadata.labels.node-role\.kubernetes\.io/control-plane,VERSION:.status.nodeInfo.kubeletVersion' \
    --no-headers
