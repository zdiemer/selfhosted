#!/usr/bin/env bash
# Configure APT on cluster nodes; does not install packages or restart apt jobs.
# Usage: apt-harden.sh --all | NODE [NODE ...]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_common.sh"
if [[ "${1:-}" == --all && $# == 1 ]]; then
    get_all_nodes
elif [[ $# -gt 0 && "$1" != -* ]]; then
    NODE_NAMES=("$@")
else
    echo "Usage: $0 --all | NODE [NODE ...]" >&2
    exit 2
fi
for node in "${NODE_NAMES[@]}"; do
    echo "Configuring APT on ${node}"
    $SSH_CMD "root@${node}" 'bash -s' <<'REMOTE'
set -euo pipefail
install -d /etc/systemd/system/apt-daily.service.d /etc/systemd/system/apt-daily-upgrade.service.d
cat > /etc/apt/apt.conf.d/99-cluster-reliability <<'CONF'
// Bound stalled downloads; keep existing origin, kernel and reboot policy.
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
Acquire::Retries "3";
// Partial index refreshes must not be reported as successful.
APT::Update::Error-Mode "any";
// Record periodic outcomes in the journal (default verbosity hides errors).
APT::Periodic::Verbose "1";
CONF
cat > /etc/systemd/system/apt-daily.service.d/cluster-reliability.conf <<'CONF'
[Service]
# This unit refreshes/downloads only; never apply this limit to installation.
TimeoutStartSec=30min
CONF
cat > /etc/systemd/system/apt-daily-upgrade.service.d/cluster-reliability.conf <<'CONF'
[Service]
# apt.systemd.daily otherwise exits 0 when its initial apt-get check fails.
ExecStartPre=/usr/bin/apt-get check
CONF
systemctl daemon-reload
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer
apt-config dump | grep -E 'Acquire::(http|https)::Timeout|Acquire::Retries|APT::Update::Error-Mode'
systemctl show apt-daily.service -p TimeoutStartUSec
REMOTE
done
