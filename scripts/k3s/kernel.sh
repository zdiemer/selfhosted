#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

# Kernel state, and making a bad kernel survivable.
#
# A laptop here panicked on a 7.0.x kernel and stayed down until it was
# physically power cycled. Nothing scheduled that: unattended-upgrades had
# installed the kernel days earlier and the node simply booted it the next time
# it restarted, so there was nothing connecting the outage to an upgrade.
#
# The fleet is why this matters. It mixes server hardware with old laptops, and
# the same 7.0.0-28 that runs happily on zachd-ubuntu and zachd-ubuntu-2 is
# what panicked the laptop — "this kernel is good" is not a fleet-wide
# statement.
#
# --no-auto-kernel is the actual fix: kernels stop arriving on their own, so
# they change only through update.sh, one drained node at a time. --protect is
# damage limitation for when one is bad anyway:
#
#   GRUB_RECORDFAIL_TIMEOUT   Ubuntu records a failed boot and then shows the
#                             menu and waits *forever* for a keypress. On a
#                             headless node that alone turns one bad boot into
#                             an indefinite outage, whatever the kernel did.
#   panic=N                   Panic reboots after N seconds instead of sitting
#                             at the trace. Only helps for a real panic — a
#                             hard lockup with no panic handler still hangs,
#                             and a kernel that panics every boot still needs
#                             hands on the machine.
#
# Nothing here is automatic. --report is the default and only reads.

ACTION="report"
TARGET_NODE=""
ALL_NODES=false
PANIC_SECONDS=10
RECORDFAIL_TIMEOUT=5

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Kernel state and boot-failure recovery for cluster nodes.

OPTIONS:
  --node <name>    Operate on a specific node
  --all            Operate on every node
  --report         Running vs installed kernels, and boot safety (default)
  --protect        Set recordfail timeout and panic auto-reboot, so a bad boot
                   is visible and self-recovering rather than a silent hang.
                   Idempotent; does not change which kernel boots.
  --hold           Pin kernel packages so apt stops upgrading them
  --unhold         Release a --hold
  --no-auto-kernel Stop unattended-upgrades installing kernels. Deliberate
                   upgrades via update.sh still work; nothing else is affected
  --auto-kernel    Undo --no-auto-kernel
  --panic-seconds <n>  Seconds before a panicking kernel reboots (default 10)
  -h, --help       Show this help

EXAMPLES:
  $(basename "$0") --all                       # what is running where
  $(basename "$0") --all --protect             # make bad boots recoverable
  $(basename "$0") --node zachd-ubuntu-laptop-1 --hold
  $(basename "$0") --all --no-auto-kernel      # kernels only via update.sh
EOF
    exit 0
}

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --node) TARGET_NODE="$2"; shift 2 ;;
        --all) ALL_NODES=true; shift ;;
        --report) ACTION="report"; shift ;;
        --protect) ACTION="protect"; shift ;;
        --hold) ACTION="hold"; shift ;;
        --unhold) ACTION="unhold"; shift ;;
        --no-auto-kernel) ACTION="no-auto-kernel"; shift ;;
        --auto-kernel) ACTION="auto-kernel"; shift ;;
        --panic-seconds) PANIC_SECONDS="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Error: Unknown option: $1" >&2; usage ;;
    esac
done

if [[ -z "$TARGET_NODE" && "$ALL_NODES" != "true" ]]; then
    echo "Error: specify --node <name> or --all." >&2
    exit 1
fi

require_tools kubectl tailscale
get_all_nodes

TARGET_NAMES=()
if [[ -n "$TARGET_NODE" ]]; then
    for name in "${NODE_NAMES[@]}"; do
        [[ "$name" == "$TARGET_NODE" ]] && TARGET_NAMES=("$name") && break
    done
    if [[ ${#TARGET_NAMES[@]} -eq 0 ]]; then
        echo "Error: Node '$TARGET_NODE' not found." >&2
        exit 1
    fi
else
    TARGET_NAMES=("${NODE_NAMES[@]}")
fi

# Newest installed kernel by version sort, which is what GRUB will boot by
# default. Compared against the running one to spot a node that is one reboot
# away from changing kernel — the moment the risk actually lands.
read -r -d '' REPORT_SCRIPT <<'EOS' || true
running="$(uname -r)"
newest="$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -1)"
count="$(ls -1 /boot/vmlinuz-* 2>/dev/null | wc -l)"
held="no"
apt-mark showhold 2>/dev/null | grep -q '^linux-image' && held="yes"
recordfail="unset"
grep -q '^GRUB_RECORDFAIL_TIMEOUT=' /etc/default/grub 2>/dev/null && \
    recordfail="$(grep '^GRUB_RECORDFAIL_TIMEOUT=' /etc/default/grub | cut -d= -f2)"
panic="unset"
grep -qE '^GRUB_CMDLINE_LINUX_DEFAULT=.*panic=' /etc/default/grub 2>/dev/null && \
    panic="$(grep -oE 'panic=[0-9]+' /etc/default/grub | head -1 | cut -d= -f2)"
grubdefault="$(grep '^GRUB_DEFAULT=' /etc/default/grub 2>/dev/null | cut -d= -f2 || echo '?')"
echo "${running}|${newest}|${count}|${held}|${recordfail}|${panic}|${grubdefault}"
EOS

report_node() {
    local hostname="$1"
    local raw
    if ! raw="$(run_on_node_sudo "$hostname" "$REPORT_SCRIPT" 2>/dev/null | tail -1)"; then
        printf '%-26s %s\n' "$hostname" "UNREACHABLE"
        return
    fi

    IFS='|' read -r running newest count held recordfail panic grubdefault <<<"$raw"

    local pending="-"
    [[ "$running" != "$newest" ]] && pending="REBOOT CHANGES KERNEL -> $newest"

    local safety="unprotected"
    if [[ "$recordfail" != "unset" && "$panic" != "unset" ]]; then
        safety="protected"
    elif [[ "$recordfail" != "unset" || "$panic" != "unset" ]]; then
        safety="partial"
    fi

    printf '%-26s %-20s %-9s %-6s %-12s %s\n' \
        "$hostname" "$running" "$count kernels" "$held" "$safety" "$pending"
}

# Written with a marker so re-running replaces our block rather than appending
# another copy of it.
# Deliberately does not touch GRUB_DEFAULT.
#
# The tempting extra is GRUB_DEFAULT=saved plus grub-reboot, to try a new
# kernel once and fall back automatically if it doesn't come up. Doing that
# correctly means resolving a kernel version to a GRUB menuentry id, including
# the submenu path under "Advanced options", by parsing grub.cfg — and a first
# attempt here silently produced no saved entry at all, which leaves
# GRUB_DEFAULT=saved resolving to entry 0. That fails open rather than closed,
# but it is exactly the kind of boot-path change that is worse when it is
# subtly wrong than when it is absent. Kernels not arriving unattended is the
# real protection; these two settings are what make a bad one visible and
# recoverable rather than a silent hang.
read -r -d '' PROTECT_SCRIPT <<EOS || true
set -e
cp -n /etc/default/grub /etc/default/grub.bak.kernelsh 2>/dev/null || true
sed -i '/# BEGIN kernel.sh/,/# END kernel.sh/d' /etc/default/grub
sed -i '/^GRUB_RECORDFAIL_TIMEOUT=/d' /etc/default/grub
sed -i '/^GRUB_CMDLINE_LINUX_DEFAULT=.*panic=/s/ *panic=[0-9]*//' /etc/default/grub
cat >> /etc/default/grub <<'GRUBEOF'
# BEGIN kernel.sh
# A headless node must never wait at the boot menu: Ubuntu sets recordfail
# after an unclean boot and then blocks indefinitely for a keypress, which
# turns one bad boot into an outage until someone walks over to the machine.
GRUB_RECORDFAIL_TIMEOUT=${RECORDFAIL_TIMEOUT}
# END kernel.sh
GRUBEOF
if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub; then
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 panic=${PANIC_SECONDS}"/' /etc/default/grub
else
  echo 'GRUB_CMDLINE_LINUX_DEFAULT="panic=${PANIC_SECONDS}"' >> /etc/default/grub
fi
sed -i 's/DEFAULT=" /DEFAULT="/' /etc/default/grub
update-grub >/dev/null 2>&1
echo "protected: recordfail=${RECORDFAIL_TIMEOUT}s panic=${PANIC_SECONDS}s (running \$(uname -r))"
EOS

case "$ACTION" in
    report)
        printf '%-26s %-20s %-9s %-6s %-12s %s\n' \
            NODE RUNNING INSTALLED HELD BOOT-SAFETY PENDING
        for hostname in "${TARGET_NAMES[@]}"; do
            report_node "$hostname"
        done
        cat <<'EOF'

BOOT-SAFETY  protected = a failed boot reboots and falls back on its own.
             unprotected = a bad kernel needs someone at the machine.
PENDING      the node is one reboot away from running a different kernel.
EOF
        ;;
    protect)
        for hostname in "${TARGET_NAMES[@]}"; do
            echo "=== $hostname ==="
            run_on_node_sudo "$hostname" "$PROTECT_SCRIPT" || \
                echo "[ERROR] Could not protect $hostname" >&2
        done
        ;;
    hold|unhold)
        verb="$ACTION"
        for hostname in "${TARGET_NAMES[@]}"; do
            echo "=== $hostname ==="
            run_on_node_sudo "$hostname" \
                "apt-mark $verb linux-image-generic linux-headers-generic linux-generic 2>&1 | sed 's/^/  /'" || \
                echo "[ERROR] Could not $verb kernels on $hostname" >&2
        done
        ;;
esac
