#!/usr/bin/env bash
# Weekly "what's stale" report for the node lane — the upgrades no PR can do.
#
# Per node (over tailscale ssh, like every script here): pending apt upgrades
# (and how many are security), running vs newest-installed kernel + whether a
# reboot is pending, k3s version, tailscale version. Cluster-wide: the k3s
# stable channel and the latest tailscale release, so drift is visible.
#
# Publishes two places, neither of them SMS (SMS stays the failure channel —
# the systemd unit's OnFailure covers that):
#   1. A ConfigMap (versions-report, namespace infra) that the cluster-status
#      collector embeds into status.json → the "Pending upgrades" panel on
#      status.diemer.codes.
#   2. A comment on the pinned "Homelab versions report" GitHub issue, so the
#      Saturday review is one glance next to Renovate's dependency dashboard.
#
# Acting on the report stays manual and uses the existing tools:
#   apt/reboots:  scripts/k3s/update.sh --reboot   (kernel.sh for the guards)
#   k3s:          scripts/k3s/k3s-upgrade.sh
#   tailscale:    rides apt (pkgs.tailscale.com is in each node's sources)
#
# Usage: versions-report.sh [--dry-run]
#   --dry-run   print the markdown report; publish nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_common.sh"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

NAMESPACE="${NAMESPACE:-infra}"
CONFIGMAP="${CONFIGMAP:-versions-report}"
ISSUE_TITLE="${ISSUE_TITLE:-Homelab versions report}"
REPO="${REPO:-zdiemer/selfhosted}"

# --- cluster-wide facts -------------------------------------------------------

# The channel URL 302s to the release tag; the final path segment is the version.
K3S_STABLE="$(curl -fsSL -o /dev/null -w '%{url_effective}' https://update.k3s.io/v1-release/channels/stable 2>/dev/null | sed 's|.*/||')" || K3S_STABLE=""
TAILSCALE_LATEST="$(curl -fsSL https://api.github.com/repos/tailscale/tailscale/releases/latest 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))' 2>/dev/null)" || TAILSCALE_LATEST=""

# --- per-node collection ------------------------------------------------------

# One ssh round-trip per node, kernel.sh-style pipe-separated record:
#   apt_pending|apt_security|reboot_required|kernel_running|kernel_newest|k3s|tailscale|ts_apt
COLLECT='
apt-get update -qq >/dev/null 2>&1 || true
sim="$(apt-get -s dist-upgrade 2>/dev/null)"
pending="$(grep -c "^Inst " <<<"$sim" || true)"
security="$(grep -c "^Inst .*-security" <<<"$sim" || true)"
reboot=no; [ -f /var/run/reboot-required ] && reboot=yes
running="$(uname -r)"
newest="$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed "s|.*/vmlinuz-||" | sort -V | tail -1)"
k3s="$(k3s --version 2>/dev/null | head -1 | awk "{print \$3}")"
ts="$(tailscale version 2>/dev/null | head -1)"
tsapt=no; ls /etc/apt/sources.list.d/ 2>/dev/null | grep -qi tailscale && tsapt=yes
guard=no; grep -qs "Automatic-Reboot .false." /etc/apt/apt.conf.d/50unattended-upgrades-local && guard=yes
echo "${pending}|${security}|${reboot}|${running}|${newest}|${k3s}|${ts}|${tsapt}|${guard}"
'

get_all_nodes
ROWS=()
UNREACHABLE=()
for node in "${NODE_NAMES[@]}"; do
    echo "==> ${node}" >&2
    if raw="$(timeout 90 $SSH_CMD "root@${node}" "$COLLECT" 2>/dev/null | tail -1)" && [[ "$raw" == *"|"* ]]; then
        ROWS+=("${node}|${raw}")
    else
        UNREACHABLE+=("$node")
        echo "    unreachable" >&2
    fi
done

# --- assemble json + markdown -------------------------------------------------

REPORT_JSON="$(python3 - "$K3S_STABLE" "$TAILSCALE_LATEST" "${ROWS[@]}" <<'EOF'
import json, sys, time
k3s_stable, ts_latest, *rows = sys.argv[1:]
nodes = []
for row in rows:
    n, pending, security, reboot, running, newest, k3s, ts, tsapt, guard = row.split("|")
    nodes.append({
        "name": n,
        "aptPending": int(pending or 0),
        "aptSecurity": int(security or 0),
        "rebootRequired": reboot == "yes",
        "kernelRunning": running,
        "kernelNewest": newest,
        "k3s": k3s,
        "tailscale": ts,
        "tailscaleViaApt": tsapt == "yes",
        # join-cluster.sh writes 50unattended-upgrades-local (Automatic-Reboot
        # false + kernel blacklist); kernel.sh --no-auto-kernel repairs it.
        # A node without it can take an unattended kernel and boot it later.
        "autoRebootGuard": guard == "yes",
    })
print(json.dumps({
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "k3sStable": k3s_stable or None,
    "tailscaleLatest": ts_latest or None,
    "nodes": nodes,
}, indent=1))
EOF
)"

MARKDOWN="$(python3 - "${UNREACHABLE[@]+"${UNREACHABLE[@]}"}" <<EOF
import json, sys
r = json.loads('''${REPORT_JSON}''')
unreachable = sys.argv[1:]
lines = [
    f"### {r['generatedAt']}",
    "",
    f"k3s stable: \`{r['k3sStable']}\` · tailscale latest: \`{r['tailscaleLatest']}\`",
    "",
    "| node | apt (sec) | reboot | kernel running → newest | k3s | tailscale |",
    "|---|---|---|---|---|---|",
]
for n in r["nodes"]:
    kern = n["kernelRunning"] if n["kernelRunning"] == n["kernelNewest"] else f"**{n['kernelRunning']} → {n['kernelNewest']}**"
    k3s = n["k3s"] if n["k3s"] == r["k3sStable"] else f"**{n['k3s']}**"
    ts = n["tailscale"] if n["tailscale"] == r["tailscaleLatest"] else f"**{n['tailscale']}**"
    apt = f"{n['aptPending']} ({n['aptSecurity']})" if n["aptPending"] else "0"
    if n["aptSecurity"]: apt = f"**{apt}**"
    lines.append(f"| {n['name']} | {apt} | {'**yes**' if n['rebootRequired'] else 'no'} | {kern} | {k3s} | {ts} |")
for n in unreachable:
    lines.append(f"| {n} | — | — | unreachable | — | — |")
noapt = [n["name"] for n in r["nodes"] if not n["tailscaleViaApt"]]
if noapt:
    lines += ["", f"⚠️ tailscale NOT apt-managed on: {', '.join(noapt)} — apt upgrades won't touch it there"]
noguard = [n["name"] for n in r["nodes"] if not n["autoRebootGuard"]]
if noguard:
    lines += ["", f"⚠️ unattended-upgrades reboot/kernel guard MISSING on: {', '.join(noguard)} — fix with \`kernel.sh --no-auto-kernel\`"]
lines += ["", "Act with \`update.sh --reboot\` / \`k3s-upgrade.sh\`; kernels via \`kernel.sh\`. Bold = drift."]
print("\n".join(lines))
EOF
)"

if $DRY_RUN; then
    echo "$MARKDOWN"
    exit 0
fi

# --- publish: ConfigMap for the status page -----------------------------------

kubectl create configmap "$CONFIGMAP" -n "$NAMESPACE" \
    --from-literal=report.json="$REPORT_JSON" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "==> ConfigMap ${NAMESPACE}/${CONFIGMAP} updated"

# --- publish: comment on the pinned issue -------------------------------------

if command -v gh >/dev/null 2>&1; then
    issue="$(gh issue list -R "$REPO" --state open --search "in:title \"${ISSUE_TITLE}\"" --json number,title \
        --jq ".[] | select(.title == \"${ISSUE_TITLE}\") | .number" | head -1)"
    if [[ -z "$issue" ]]; then
        issue="$(gh issue create -R "$REPO" --title "$ISSUE_TITLE" \
            --body "Weekly node-lane report: pending apt/security updates, kernel drift, k3s vs stable, tailscale spread. Posted by scripts/k3s/versions-report.sh (systemd timer, Sat 07:00). Newest comment = current state." \
            | sed 's|.*/||')"
        echo "==> created issue #${issue}"
    fi
    gh issue comment "$issue" -R "$REPO" --body "$MARKDOWN" >/dev/null
    echo "==> commented on issue #${issue}"
else
    echo "WARN: gh not available — issue comment skipped (ConfigMap still updated)" >&2
fi
