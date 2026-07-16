#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

TARGET_ALL=false
TARGET_NODE=""
DRY_RUN=false
DEEP=false
REPORT=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Clean up disk space on k3s cluster nodes.

OPTIONS:
  --all            Run on all nodes
  --node <name>    Run on a specific node
  --deep           Aggressive cleanup: remove all images, GC orphaned
                   containerd snapshots, and nuke Docker state
                   (buildkit cache + docker system prune --volumes).
                   Pods will re-pull, Tilt builds will be slower.
  --report         Print a disk-usage report for each target node and
                   exit. Makes no changes. Useful for pinpointing
                   which directory is consuming space.
  --dry-run        Show what would be done without executing
  -h, --help       Show this help message

EXAMPLES:
  $(basename "$0") --all
  $(basename "$0") --node mynode
  $(basename "$0") --all --dry-run
  $(basename "$0") --all --deep
  $(basename "$0") --all --report
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
        --deep)
            DEEP=true
            shift
            ;;
        --report)
            REPORT=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
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

if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY RUN] No changes will be made."
    echo ""
fi

# Fetch the cluster-wide list of valid PV names once so the per-node
# report can flag orphaned local-path directories (PV deleted, data
# left on disk because reclaim policy was Retain or kubelet skipped
# cleanup).
VALID_PVS=""
if [[ "$REPORT" == "true" ]]; then
    VALID_PVS=$(kubectl get pv \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
        2>/dev/null | sort -u || echo "")
fi

for i in "${!TARGET_NAMES[@]}"; do
    hostname="${TARGET_NAMES[$i]}"
    echo "=== Cleaning $hostname ==="

    if ! is_local_node "$hostname" \
        && ! $SSH_CMD "$hostname" "echo ok" >/dev/null; then
        echo "[ERROR] $hostname is unreachable, skipping."
        echo ""
        continue
    fi

    if [[ "$REPORT" == "true" ]]; then
        echo "--- Disk Usage ---"
    else
        echo "--- Disk Usage (Before) ---"
    fi
    print_disk_usage "$hostname" || true

    if [[ "$REPORT" == "true" ]]; then
        echo ""
        echo "--- Journal ---"
        run_on_node_sudo "$hostname" \
            "journalctl --disk-usage 2>/dev/null || true"

        echo "--- Containerd (system, /var/lib/containerd) ---"
        run_on_node_sudo "$hostname" \
            "test -d /var/lib/containerd && du -sh /var/lib/containerd/io.containerd.* 2>/dev/null || echo '(not present)'"

        echo "--- Containerd (k3s) ---"
        run_on_node_sudo "$hostname" \
            "K3S=/var/lib/rancher/k3s/agent/containerd; test -d \"\$K3S\" && du -sh \"\$K3S\"/io.containerd.* 2>/dev/null || echo '(not present)'"

        echo "--- Docker ---"
        run_on_node_sudo "$hostname" \
            "command -v docker >/dev/null && docker system df || echo '(docker not installed)'"

        echo "--- Buildkit cache ---"
        run_on_node_sudo "$hostname" \
            "command -v docker >/dev/null && docker buildx du 2>/dev/null | tail -5 || echo '(docker not installed)'"

        echo "--- Pod logs ---"
        run_on_node_sudo "$hostname" \
            "du -sh /var/log/pods /var/log/containers 2>/dev/null || true"

        echo "--- Crash dumps ---"
        run_on_node_sudo "$hostname" \
            "du -sh /var/lib/systemd/coredump /var/crash 2>/dev/null || true"

        echo "--- Snap revisions ---"
        run_on_node_sudo "$hostname" \
            "test -d /var/lib/snapd && du -sh /var/lib/snapd 2>/dev/null || echo '(snapd not installed)'"

        echo "--- /var/tmp ---"
        run_on_node_sudo "$hostname" \
            "du -sh /var/tmp 2>/dev/null || true"

        echo "--- Top /var/lib consumers ---"
        run_on_node_sudo "$hostname" \
            "du -h --max-depth=1 /var/lib 2>/dev/null | sort -h | tail -10"

        echo "--- Top /var/log consumers ---"
        run_on_node_sudo "$hostname" \
            "du -h --max-depth=1 /var/log 2>/dev/null | sort -h | tail -10"

        echo "--- Containerd leases (k8s.io) ---"
        run_on_node_sudo "$hostname" \
            "k3s ctr -n k8s.io leases list 2>/dev/null || true"

        echo "--- k3s local PV usage ---"
        # local-path-provisioner stores PV data at
        # /var/lib/rancher/k3s/storage/<pv>_<ns>_<pvc>/.
        # Capacity is advisory, so this is the only honest view of
        # which PVCs are actually consuming the underlying disk.
        pv_listing=$(run_on_node_sudo "$hostname" \
            "test -d /var/lib/rancher/k3s/storage && du -sh /var/lib/rancher/k3s/storage/pvc-* 2>/dev/null || echo '__NONE__'") || true
        if [[ -z "$pv_listing" || "$pv_listing" == "__NONE__" ]]; then
            echo "(no local-path PV storage on this node)"
        else
            while IFS=$'\t' read -r pv_size pv_path; do
                [[ -z "$pv_size" ]] && continue
                pv_dir=$(basename "$pv_path")
                pv_name="${pv_dir%%_*}"
                if [[ -n "$VALID_PVS" ]] \
                    && ! grep -qFx "$pv_name" <<< "$VALID_PVS"; then
                    echo "  $pv_size  $pv_dir [ORPHAN]"
                else
                    echo "  $pv_size  $pv_dir"
                fi
            done <<< "$pv_listing"
        fi

        echo ""
        continue
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        echo ""
        echo "[DRY RUN] Would run on $hostname:"
        echo "  - k3s crictl rm (stopped containers)"
        echo "  - k3s crictl rmi --prune"
        echo "  - k3s ctr content prune references"
        echo "  - docker volume prune -a -f"
        echo "  - docker image prune -a -f"
        echo "  - apt-get autoremove -y && apt-get clean"
        echo "  - journalctl --vacuum-time=7d"
        echo "  - Clean k3s temp/ingest files"
        if [[ "$DEEP" == "true" ]]; then
            echo "  - [DEEP] k3s crictl rmi --all"
            echo "  - [DEEP] Remove unreferenced snapshots (via k3s ctr)"
            echo "  - [DEEP] docker buildx prune -a -f (buildkit cache)"
            echo "  - [DEEP] docker system prune -a -f --volumes"
        fi
        echo ""
        continue
    fi

    echo "--- Removing stopped containers ---"
    run_on_node_sudo "$hostname" \
        "EXITED=\$(k3s crictl ps -a -q --state exited 2>/dev/null); [ -n \"\$EXITED\" ] && k3s crictl rm \$EXITED || true"

    echo "--- Pruning container images ---"
    run_on_node_sudo "$hostname" \
        "k3s crictl rmi --prune 2>/dev/null || true"

    echo "--- Pruning containerd content ---"
    run_on_node_sudo "$hostname" \
        "k3s ctr -n k8s.io content prune references 2>/dev/null || true"

    echo "--- Pruning Docker volumes ---"
    run_on_node_sudo "$hostname" \
        "docker volume prune -a -f 2>/dev/null || true"

    echo "--- Pruning Docker images ---"
    run_on_node_sudo "$hostname" \
        "docker image prune -a -f 2>/dev/null || true"

    echo "--- Cleaning apt caches ---"
    run_on_node_sudo "$hostname" \
        "apt-get autoremove -y && apt-get clean"

    echo "--- Vacuuming journal logs ---"
    run_on_node_sudo "$hostname" \
        "journalctl --vacuum-time=7d"

    echo "--- Cleaning k3s temp files ---"
    run_on_node_sudo "$hostname" \
        "rm -rf /var/lib/rancher/k3s/agent/containerd/io.containerd.content.v1.content/ingest/* 2>/dev/null; rm -rf /tmp/k3s-* /tmp/k3d-* 2>/dev/null || true"

    if [[ "$DEEP" == "true" ]]; then
        echo "--- [DEEP] Removing all container images ---"
        run_on_node_sudo "$hostname" \
            "k3s crictl rmi --all 2>/dev/null || true"

        echo "--- [DEEP] Removing unreferenced snapshots ---"
        # shellcheck disable=SC2016  # expansions run on the remote shell
        run_on_node_sudo "$hostname" '
REMOVED=0
SKIPPED=0
SNAPS=$(k3s ctr -n k8s.io snapshots ls 2>/dev/null | awk "NR>1 {print \$1}")
for snap in $SNAPS; do
    if k3s ctr -n k8s.io snapshots rm "$snap" 2>/dev/null; then
        REMOVED=$((REMOVED + 1))
    else
        SKIPPED=$((SKIPPED + 1))
    fi
done
echo "Removed $REMOVED unreferenced snapshots ($SKIPPED still in use)"
'

        echo "--- [DEEP] Pruning Docker buildkit cache ---"
        run_on_node_sudo "$hostname" \
            "docker buildx prune -a -f 2>/dev/null || true"

        echo "--- [DEEP] Docker system prune (nuclear) ---"
        run_on_node_sudo "$hostname" \
            "docker system prune -a -f --volumes 2>/dev/null || true"
    fi

    echo "--- Disk Usage (After) ---"
    print_disk_usage "$hostname" || true

    echo "[SUCCESS] Cleanup complete on $hostname"
    echo ""
done

if [[ "$REPORT" == "true" ]]; then
    echo "=== Report Complete ==="
else
    echo "=== Cleanup Complete ==="
fi
