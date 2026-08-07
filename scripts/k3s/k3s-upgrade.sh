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
Upgrades servers first, then agents — a kubelet may run at or behind
the control plane, never ahead. Re-runs the k3s installer with the
specified version on each node.

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
    # No -L here. The channel endpoint answers 302 to the release tag, and the
    # version is read off that Location header — but -L makes curl follow the
    # redirect, so %{redirect_url} comes back empty and this path could never
    # resolve a version at all. Ask for the 302 and read it.
    TARGET_VERSION=$(curl -s \
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

# Kubernetes supports stepping the control plane one minor at a time. The k3s
# stable channel runs well ahead of this cluster, so a bare `k3s-upgrade.sh`
# with no --version can silently propose a two-minor jump (1.34 -> 1.36), which
# breaks etcd and the apiserver. Refuse it and name the intermediate hop.
_minor_of() { sed -E 's/^v([0-9]+)\.([0-9]+).*/\1 \2/' <<< "$1"; }

lowest_node_version=""
for name in "${NODE_NAMES[@]}"; do
    v=$(kubectl get node "$name" \
        -o jsonpath='{.status.nodeInfo.kubeletVersion}' 2>/dev/null || echo "")
    [[ -n "$v" ]] || continue
    if [[ -z "$lowest_node_version" ]] \
        || [[ "$(printf '%s\n%s\n' "$v" "$lowest_node_version" \
                 | sort -V | head -1)" == "$v" ]]; then
        lowest_node_version="$v"
    fi
done

if [[ -n "$lowest_node_version" ]]; then
    read -r t_maj t_min <<< "$(_minor_of "$TARGET_VERSION")"
    read -r l_maj l_min <<< "$(_minor_of "$lowest_node_version")"
    if [[ "$t_maj" == "$l_maj" ]] && (( t_min - l_min > 1 )); then
        cat >&2 <<EOF
Error: $TARGET_VERSION is $(( t_min - l_min )) minor versions ahead of the
oldest node in the cluster ($lowest_node_version).

Kubernetes supports one minor step at a time. Upgrade in stages, letting the
cluster settle between them:

  $(basename "$0") --version v${l_maj}.$(( l_min + 1 )).<latest patch>
  $(basename "$0") --version $TARGET_VERSION

Pass --version explicitly to override this check with a nearer target.
EOF
        exit 1
    fi
fi

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
    sort_nodes_servers_first
    TARGET_NAMES=("${NODE_NAMES[@]}")
fi

# Whole-cluster runs are ordered servers-first, so they can't invert the skew.
# A --node run can: upgrading one agent past the control plane is exactly how
# zachd-ubuntu-2 reached v1.35.4 against a v1.34.3 server. Refuse that here.
if [[ -n "$TARGET_NODE" ]] \
    && [[ "$(get_node_role "$TARGET_NODE")" != "server" ]]; then
    lowest_server=""
    for name in "${NODE_NAMES[@]}"; do
        [[ "$(get_node_role "$name")" == "server" ]] || continue
        v=$(kubectl get node "$name" \
            -o jsonpath='{.status.nodeInfo.kubeletVersion}' 2>/dev/null || echo "")
        [[ -n "$v" ]] || continue
        if [[ -z "$lowest_server" ]] \
            || [[ "$(printf '%s\n%s\n' "$v" "$lowest_server" \
                     | sort -V | head -1)" == "$v" ]]; then
            lowest_server="$v"
        fi
    done

    # sort -V orders v1.34.3+k3s3 < v1.34.6+k3s1 < v1.35.4+k3s1 correctly.
    if [[ -n "$lowest_server" ]] \
        && [[ "$(printf '%s\n%s\n' "$TARGET_VERSION" "$lowest_server" \
                 | sort -V | tail -1)" == "$TARGET_VERSION" ]] \
        && [[ "$TARGET_VERSION" != "$lowest_server" ]]; then
        cat >&2 <<EOF
Error: refusing to upgrade agent '$TARGET_NODE' to $TARGET_VERSION.

  Lowest control-plane version: $lowest_server

A kubelet must run at or behind the control plane, never ahead. Upgrade the
servers first:

  $(basename "$0") --version $TARGET_VERSION

EOF
        exit 1
    fi
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
