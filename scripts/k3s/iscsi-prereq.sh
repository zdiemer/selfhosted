#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

# Retrofit the storage client packages onto nodes that were joined before
# join-cluster.sh started installing them. New nodes get this from bootstrap
# (~/Code/k3s-cluster, Step 7); this script is for the ones already in the
# cluster.
#
# open-iscsi backs the truenas-iscsi StorageClass (RWO zvols), nfs-common backs
# truenas-nfs (RWX datasets). A node missing either looks perfectly healthy and
# then fails to mount the moment a stateful pod lands on it — which is why this
# has to run across every node BEFORE any PVC is migrated.
#
# Read-only by default. Nothing is installed without --apply.

APPLY=false
TARGET_NODE=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Install and configure the iSCSI/NFS client packages on k3s cluster nodes.
Reports what is missing by default; use --apply to change anything.

OPTIONS:
  --apply          Actually install and configure (default: report only)
  --node <name>    Operate on a specific node only
  -h, --help       Show this help message

EXAMPLES:
  $(basename "$0")                 # audit every node, change nothing
  $(basename "$0") --apply         # install everywhere that's missing it
  $(basename "$0") --node zachd-ubuntu-2 --apply
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --apply)
            APPLY=true
            shift
            ;;
        --node)
            TARGET_NODE="$2"
            shift 2
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

# _common.sh routes the local node through `sudo bash -c` rather than SSH. In a
# non-interactive shell that fails on a password prompt and looks identical to an
# unreachable host, so check up front and say which it is.
if ! sudo -n true 2>/dev/null; then
    echo "[WARNING] No passwordless sudo here ($(hostname))."
    echo "          This node will report as unreachable. Re-run with sudo, or"
    echo "          use --node to target the others."
    echo ""
fi

if [[ -n "$TARGET_NODE" ]]; then
    NODE_NAMES=("$TARGET_NODE")
else
    get_all_nodes
fi

if [[ "$APPLY" != "true" ]]; then
    echo "=== AUDIT ONLY — nothing will be changed (use --apply) ==="
else
    echo "=== APPLYING storage client prerequisites ==="
fi
echo ""

# IQN per node, so duplicates can be caught after the loop. A node can't detect
# a collision on its own; only this cluster-wide view can.
declare -A NODE_IQNS=()
MISSING_NODES=()
FAILED_NODES=()

for hostname in "${NODE_NAMES[@]}"; do
    echo "--- $hostname ---"

    # Only the IQN read needs root (/etc/iscsi/initiatorname.iscsi is mode 600).
    # Everything else is world-readable, so the audit runs unprivileged and stays
    # useful on a node without passwordless sudo — which includes whichever node
    # you happen to be sitting on. Auditing must not require what fixing requires,
    # or democratic-csi/upgrade.sh's preflight would prompt on every run.
    RUNNER=run_on_node_sudo
    if is_local_node "$hostname" && ! sudo -n true 2>/dev/null; then
        RUNNER=run_on_node
    fi

    status=$($RUNNER "$hostname" '
        for pkg in open-iscsi nfs-common; do
            if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
                echo "pkg:$pkg=ok"
            else
                echo "pkg:$pkg=missing"
            fi
        done
        # Verbatim, not a boolean. "failed" is a different problem from
        # "inactive" — zachd-ubuntu had iscsid oom-killed with the packages
        # correctly installed, which a yes/no check would have reported as a
        # simple missing-service.
        echo "iscsid=$(systemctl is-active iscsid 2>/dev/null || true)"
        echo "oom_protected=$(systemctl show iscsid -p OOMScoreAdjust --value 2>/dev/null || echo "?")"
        if [ ! -s /etc/iscsi/initiatorname.iscsi ]; then
            echo "iqn=none"
        elif [ -r /etc/iscsi/initiatorname.iscsi ]; then
            grep "^InitiatorName=" /etc/iscsi/initiatorname.iscsi | sed "s/^InitiatorName=/iqn=/"
        else
            # Present but unreadable without root. Not a fault — just excluded
            # from the cross-node uniqueness comparison below.
            echo "iqn=unreadable"
        fi
    ' 2>/dev/null) || {
        echo "  [ERROR] could not reach $hostname"
        FAILED_NODES+=("$hostname")
        echo ""
        continue
    }

    echo "$status" | sed 's/^/  /'

    node_iqn=$(echo "$status" | grep '^iqn=' | cut -d= -f2-)
    [[ "$node_iqn" != "none" && "$node_iqn" != "unreadable" ]] && NODE_IQNS["$hostname"]="$node_iqn"

    if echo "$status" | grep -qE "=missing|iscsid=(inactive|failed|unknown|)$|iqn=none|oom_protected=0"; then
        MISSING_NODES+=("$hostname")

        if [[ "$APPLY" == "true" ]]; then
            echo "  Installing..."
            # Mirrors configure_iscsi_initiator() in ~/Code/k3s-cluster.
            # Keep the two in sync; see that repo for the rationale on each
            # setting (unique IQN, automatic login, 300s replacement_timeout).
            run_on_node_sudo "$hostname" '
                set -e
                export DEBIAN_FRONTEND=noninteractive
                apt-get update -qq
                apt-get install -y open-iscsi nfs-common

                if [ ! -s /etc/iscsi/initiatorname.iscsi ] || \
                   ! grep -q "^InitiatorName=iqn\." /etc/iscsi/initiatorname.iscsi; then
                    echo "InitiatorName=$(iscsi-iname)" > /etc/iscsi/initiatorname.iscsi
                    chmod 600 /etc/iscsi/initiatorname.iscsi
                fi

                sed -i "s/^node\.startup\s*=.*/node.startup = automatic/" /etc/iscsi/iscsid.conf
                grep -q "^node\.startup" /etc/iscsi/iscsid.conf || \
                    echo "node.startup = automatic" >> /etc/iscsi/iscsid.conf

                sed -i "s/^node\.session\.timeo\.replacement_timeout\s*=.*/node.session.timeo.replacement_timeout = 300/" \
                    /etc/iscsi/iscsid.conf
                grep -q "^node\.session\.timeo\.replacement_timeout" /etc/iscsi/iscsid.conf || \
                    echo "node.session.timeo.replacement_timeout = 300" >> /etc/iscsi/iscsid.conf

                mkdir -p /etc/systemd/system/iscsid.service.d
                printf "[Service]\nOOMScoreAdjust=-1000\n" \
                    > /etc/systemd/system/iscsid.service.d/oom.conf
                systemctl daemon-reload

                systemctl enable --now iscsid open-iscsi
                systemctl reset-failed iscsid 2>/dev/null || true
                systemctl restart iscsid
                grep "^InitiatorName=" /etc/iscsi/initiatorname.iscsi
            ' || {
                echo "  [ERROR] install failed on $hostname"
                FAILED_NODES+=("$hostname")
                echo ""
                continue
            }
            echo "  [OK] configured"

            NODE_IQNS["$hostname"]=$(run_on_node_sudo "$hostname" \
                'grep "^InitiatorName=" /etc/iscsi/initiatorname.iscsi | cut -d= -f2-' 2>/dev/null || echo "")
        fi
    else
        echo "  [OK] already configured"
    fi
    echo ""
done

# ------------------------------------------------------------------------------
# Duplicate IQN check
# ------------------------------------------------------------------------------
# Two nodes sharing an initiator name will fight over the same iSCSI sessions and
# corrupt them. Cloning a node's disk to build another is the usual cause, and it
# is invisible from either node alone.
echo "=== Initiator IQN uniqueness ==="
if [[ ${#NODE_IQNS[@]} -eq 0 ]]; then
    echo "[WARNING] No initiator names collected — nothing to compare."
    echo "Re-run after --apply to verify uniqueness before migrating any PVC."
    DUPES=""
else
    DUPES=$(printf '%s\n' "${NODE_IQNS[@]}" | sort | uniq -d)
fi
if [[ -n "$DUPES" ]]; then
    echo "[ERROR] Duplicate initiator IQNs found — iSCSI volumes WILL corrupt:"
    while IFS= read -r dupe; do
        [[ -z "$dupe" ]] && continue
        echo "  $dupe"
        for host in "${!NODE_IQNS[@]}"; do
            [[ "${NODE_IQNS[$host]}" == "$dupe" ]] && echo "    - $host"
        done
    done <<< "$DUPES"
    echo ""
    echo "Fix by regenerating on all but one of each set:"
    echo "  tailscale ssh root@<node> \\"
    echo "    'echo \"InitiatorName=\$(iscsi-iname)\" > /etc/iscsi/initiatorname.iscsi \\"
    echo "     && systemctl restart iscsid'"
    exit 1
fi
[[ ${#NODE_IQNS[@]} -gt 0 ]] && echo "[OK] all ${#NODE_IQNS[@]} initiator names unique"
echo ""

# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------
echo "=== Summary ==="
if [[ ${#FAILED_NODES[@]} -gt 0 ]]; then
    echo "[ERROR] Failed: ${FAILED_NODES[*]}"
    exit 1
fi

if [[ ${#MISSING_NODES[@]} -eq 0 ]]; then
    echo "[OK] Every node already has the storage clients configured."
elif [[ "$APPLY" == "true" ]]; then
    echo "[OK] Configured ${#MISSING_NODES[@]} node(s): ${MISSING_NODES[*]}"
else
    echo "[WARNING] ${#MISSING_NODES[@]} node(s) need work: ${MISSING_NODES[*]}"
    echo "Re-run with --apply to install."
    exit 1
fi
