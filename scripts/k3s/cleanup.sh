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
  --deep           Aggressive cleanup: remove every image and containerd
                   snapshot that nothing on the node is standing on, and
                   nuke Docker state (buildkit cache + docker system
                   prune --volumes). Pods will re-pull, Tilt builds will
                   be slower. What running containers use is spared --
                   see "Why --deep spares images" in the README.
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
        && ! $SSH_CMD "${SSH_USER}@$hostname" "echo ok" >/dev/null; then
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
            echo "  - [DEEP] Remove images no container on the node uses"
            echo "  - [DEEP] Remove snapshots not under a running container"
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
        echo "--- [DEEP] Removing images no container on this node uses ---"
        # This was `crictl rmi --all`, which also deletes the images backing
        # RUNNING containers. On 2026-08-20 that hollowed out every pod on
        # zachd-ubuntu-2: each one kept running on inodes it already had open,
        # so nothing crashed, nothing restarted and every readiness probe
        # stayed green -- but any NEW exec got ENOENT. The claude-workspace
        # pod sat 7/7 Running for a day while its Signal/WhatsApp gateway
        # could not spawn its approval MCP server and Happy could not spawn
        # tmux or git, with errors that pointed anywhere but here. So ask the
        # node what its containers actually reference, and skip exactly those.
        # shellcheck disable=SC2016  # expansions run on the remote shell
        run_on_node_sudo "$hostname" '
if ! command -v jq >/dev/null; then
    echo "jq not found -- skipping, refusing to remove images blind"
    exit 0
fi
IN_USE=$(k3s crictl ps -a -o json 2>/dev/null \
    | jq -r ".containers[] | .imageRef, .image.image" | sort -u)
# Pod sandboxes are not containers and never appear in `crictl ps`, so the
# pause image has to be spared by name or the next pod scheduled here stalls
# on a re-pull it should not have needed.
PAUSE=$(k3s crictl images -o json 2>/dev/null \
    | jq -r ".images[] | select((.repoTags // []) | join(\",\") | test(\"pause\")) | .id")
IN_USE=$(printf "%s\n%s\n" "$IN_USE" "$PAUSE" | sort -u)
REMOVED=0
KEPT=0
for img in $(k3s crictl images -q 2>/dev/null | sort -u); do
    if printf "%s\n" "$IN_USE" | grep -qxF "$img"; then
        KEPT=$((KEPT + 1))
    elif k3s crictl rmi "$img" >/dev/null 2>&1; then
        REMOVED=$((REMOVED + 1))
    fi
done
echo "Removed $REMOVED images ($KEPT still in use)"
'

        echo "--- [DEEP] Removing snapshots nothing is standing on ---"
        # Same hazard one layer down: an Active snapshot is the live rootfs of
        # a running container, and the PARENT chain above it is the image
        # layers underneath that container. The old loop offered containerd
        # every snapshot on the node -- ~200 of them, of which six were
        # actually free -- and trusted it to refuse the rest. It does not
        # refuse all of them. Walk the chains first and only offer what no
        # Active snapshot is standing on.
        # shellcheck disable=SC2016  # expansions run on the remote shell
        run_on_node_sudo "$hostname" '
SNAPS=$(k3s ctr -n k8s.io snapshots ls 2>/dev/null)
if [ -z "$SNAPS" ]; then
    echo "(no snapshots)"
    exit 0
fi
# Rows are KEY PARENT KIND, but a root snapshot has no parent and so prints
# only two fields -- read KIND off the end, not off column three.
CANDIDATES=$(printf "%s\n" "$SNAPS" | awk "
NR == 1 { next }
{
    key = \$1
    if (NF >= 3) { parent = \$2; kind = \$3 } else { parent = \"\"; kind = \$2 }
    par[key] = parent
    keys[++n] = key
    if (kind == \"Active\") active[++a] = key
}
END {
    for (i = 1; i <= a; i++) {
        c = active[i]
        while (c != \"\" && !(c in held)) { held[c] = 1; c = par[c] }
    }
    for (i = 1; i <= n; i++) if (!(keys[i] in held)) print keys[i]
}
")
TOTAL=$(printf "%s\n" "$SNAPS" | awk "END {print NR - 1}")
REMOVED=0
SKIPPED=0
for snap in $CANDIDATES; do
    if k3s ctr -n k8s.io snapshots rm "$snap" 2>/dev/null; then
        REMOVED=$((REMOVED + 1))
    else
        SKIPPED=$((SKIPPED + 1))
    fi
done
echo "Removed $REMOVED snapshots ($SKIPPED refused, $((TOTAL - REMOVED - SKIPPED)) held by running containers)"
'

        echo "--- [DEEP] Pruning Docker buildkit cache ---"
        run_on_node_sudo "$hostname" \
            "docker buildx prune -a -f 2>/dev/null || true"

        echo "--- [DEEP] Docker system prune (nuclear) ---"
        run_on_node_sudo "$hostname" \
            "docker system prune -a -f --volumes 2>/dev/null || true"
    fi

    echo "--- Verifying running containers still have their images ---"
    # The belt to the braces above, and the check that would have caught this
    # a day earlier: a container whose image was deleted keeps running and
    # stays Ready, so nothing short of an exec tells you its rootfs is gone.
    # kubelet will not notice and will not restart it -- only a pod delete
    # (which re-pulls) fixes it.
    # shellcheck disable=SC2016  # expansions run on the remote shell
    run_on_node_sudo "$hostname" '
if ! command -v jq >/dev/null; then
    echo "(jq not found -- skipped)"
    exit 0
fi
PRESENT=$(k3s crictl images -q 2>/dev/null | sort -u)
MISSING=$(k3s crictl ps -o json 2>/dev/null \
    | jq -r ".containers[] | .imageRef + \" \" + .labels[\"io.kubernetes.pod.namespace\"] + \" \" + .labels[\"io.kubernetes.pod.name\"]" \
    | while read -r ref ns pod; do
        printf "%s\n" "$PRESENT" | grep -qxF "$ref" || echo "$ns $pod"
      done | sort -u)
if [ -n "$MISSING" ]; then
    echo "[WARNING] these pods are running on an image that is gone."
    echo "          They stay Ready and fail every new exec until deleted:"
    printf "%s\n" "$MISSING" | while read -r ns pod; do
        echo "            kubectl delete pod -n $ns $pod"
    done
else
    echo "OK - every running container still has its image."
fi
'

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
