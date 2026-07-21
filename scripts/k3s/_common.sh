# Shared helper library for k3s cluster management scripts.
# Source this file; do not execute directly.

[[ "${_COMMON_LOADED:-}" == "true" ]] && return 0
_COMMON_LOADED=true

SSH_CMD="tailscale ssh"
# User for non-sudo node commands. Bare `tailscale ssh <host>` connects as the
# invoking user, which works from a laptop (zachd exists on every node) but not
# from the claude-workspace pod (its user `node` exists nowhere) — the chart
# sets SSH_USER=root there. Sudo commands always go root@ regardless.
SSH_USER="${SSH_USER:-$(id -un)}"
DRAIN_OPTS="--ignore-daemonsets --delete-emptydir-data --timeout=300s"
NODE_READY_TIMEOUT=300

declare -a NODE_NAMES=()
declare -A NODE_ROLES=()

SELECTED_NODE_NAME=""
LOCAL_HOSTNAME=$(hostname)

is_local_node() {
    [[ "$1" == "$LOCAL_HOSTNAME" ]]
}

require_tools() {
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "Error: $tool is not installed." >&2
            exit 1
        fi
    done
}

get_all_nodes() {
    NODE_NAMES=()
    NODE_ROLES=()

    local output
    output=$(kubectl get nodes \
        -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.labels.node-role\.kubernetes\.io/master}{.metadata.labels.node-role\.kubernetes\.io/control-plane}{"\n"}{end}' \
        2>/dev/null) || true

    if [[ -z "$output" ]]; then
        echo "Error: No nodes found in k3s cluster." >&2
        exit 1
    fi

    while IFS=' ' read -r name role_label; do
        [[ -z "$name" ]] && continue
        NODE_NAMES+=("$name")
        if [[ -n "$role_label" ]]; then
            NODE_ROLES[$name]="server"
        else
            NODE_ROLES[$name]="agent"
        fi
    done <<< "$output"

    if [[ ${#NODE_NAMES[@]} -eq 0 ]]; then
        echo "Error: No nodes found in k3s cluster." >&2
        exit 1
    fi
}

get_node_role() {
    local hostname="$1"

    if [[ -n "${NODE_ROLES[$hostname]:-}" ]]; then
        echo "${NODE_ROLES[$hostname]}"
        return
    fi

    # Fallback: query kubectl directly
    local role_label
    role_label=$(kubectl get node "$hostname" \
        -o jsonpath='{.metadata.labels.node-role\.kubernetes\.io/master}{.metadata.labels.node-role\.kubernetes\.io/control-plane}' \
        2>/dev/null || echo "")

    if [[ -n "$role_label" ]]; then
        NODE_ROLES[$hostname]="server"
        echo "server"
    else
        NODE_ROLES[$hostname]="agent"
        echo "agent"
    fi
}

select_node() {
    get_all_nodes

    echo "Available nodes:"
    for i in "${!NODE_NAMES[@]}"; do
        local role="${NODE_ROLES[${NODE_NAMES[$i]}]}"
        echo "$((i + 1)). ${NODE_NAMES[$i]} ($role)"
    done

    echo ""
    while true; do
        read -rp \
            "Selection [1-${#NODE_NAMES[@]}]: " selection
        if [[ "$selection" =~ ^[0-9]+$ ]] \
            && [[ "$selection" -ge 1 ]] \
            && [[ "$selection" -le "${#NODE_NAMES[@]}" ]]; then
            local idx=$((selection - 1))
            SELECTED_NODE_NAME="${NODE_NAMES[$idx]}"
            break
        else
            echo "Invalid selection." >&2
        fi
    done
}

run_on_node() {
    local hostname="$1"
    local cmd="$2"

    if is_local_node "$hostname"; then
        if ! bash -c "$cmd"; then
            echo "[ERROR] Failed to run command on $hostname" >&2
            return 1
        fi
    else
        if ! $SSH_CMD "${SSH_USER}@$hostname" "$cmd"; then
            echo "[ERROR] Failed to run command on $hostname" >&2
            return 1
        fi
    fi
}

run_on_node_sudo() {
    local hostname="$1"
    local cmd="$2"

    if is_local_node "$hostname"; then
        if ! sudo bash -c "$cmd"; then
            echo "[ERROR] Failed to run command on $hostname" >&2
            return 1
        fi
    else
        if ! $SSH_CMD "root@$hostname" "$cmd"; then
            echo "[ERROR] Failed to run command on $hostname" >&2
            return 1
        fi
    fi
}

# Print real-disk usage for the node: every distinct block-backed
# filesystem (root, extra disks like /dev/sdb1, eMMC), with
# tmpfs/overlay/squashfs/etc filtered out and bind-mount duplicates
# collapsed by source.
print_disk_usage() {
    local hostname="$1"
    run_on_node "$hostname" \
        "df -h --local -x tmpfs -x devtmpfs -x overlay -x shm -x squashfs -x fuse.lxcfs -x efivarfs 2>/dev/null | awk 'NR==1 || !seen[\$1]++'"
}

drain_node() {
    local node_name="$1"

    echo "Draining $node_name..."
    if ! kubectl drain "$node_name" $DRAIN_OPTS; then
        echo "[WARNING] Drain failed for $node_name" >&2
        return 1
    fi
    echo "[SUCCESS] $node_name drained"
}

uncordon_node() {
    local node_name="$1"

    echo "Uncordoning $node_name..."
    kubectl uncordon "$node_name"
    echo "[SUCCESS] $node_name uncordoned"
}

wait_for_node_online() {
    local hostname="$1"
    local timeout="${2:-$NODE_READY_TIMEOUT}"
    local elapsed=0

    echo -n "Waiting for $hostname to come online"

    while [[ $elapsed -lt $timeout ]]; do
        local reachable=false
        if is_local_node "$hostname"; then
            reachable=true
        elif $SSH_CMD "${SSH_USER}@$hostname" "echo ok" >/dev/null; then
            reachable=true
        fi
        if [[ "$reachable" == "true" ]]; then
            local status
            status=$(kubectl get node "$hostname" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' \
                2>/dev/null || echo "")
            if [[ "$status" == "True" ]]; then
                echo ""
                echo "[SUCCESS] $hostname is online and ready"
                return 0
            fi
        fi
        echo -n "."
        sleep 5
        elapsed=$((elapsed + 5))
    done

    echo ""
    echo "[ERROR] Timed out waiting for $hostname" \
        "(${timeout}s)" >&2
    return 1
}

check_reboot_required() {
    local hostname="$1"

    run_on_node "$hostname" \
        "test -f /var/run/reboot-required && echo yes || echo no" \
        2>/dev/null
}

get_k3s_service_name() {
    local hostname="$1"
    local role
    role=$(get_node_role "$hostname")

    if [[ "$role" == "server" ]]; then
        echo "k3s"
    else
        echo "k3s-agent"
    fi
}

sort_nodes_agents_first() {
    local -a server_names=()
    local -a agent_names=()

    for name in "${NODE_NAMES[@]}"; do
        local role
        role=$(get_node_role "$name")
        if [[ "$role" == "server" ]]; then
            server_names+=("$name")
        else
            agent_names+=("$name")
        fi
    done

    NODE_NAMES=("${agent_names[@]}" "${server_names[@]}")
}
