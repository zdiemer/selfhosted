#!/usr/bin/env bash
# Turn a fresh Debian/Ubuntu VPS into the cluster's egress exit. Run once, as
# root, on the VPS itself. Idempotent — safe to re-run after changing settings.
#
#   scp -r vps/ root@<vps>:/tmp/egress-vps && ssh root@<vps> 'bash /tmp/egress-vps/bootstrap.sh'
#
# WHAT YOU GET
#   * squid listening for TLS proxy connections on a non-standard port
#   * password auth, so it is never an open relay
#   * an nftables allowlist that only admits the house, refreshed from DuckDNS
#   * unattended security upgrades
#
# It prints the three values to paste into infra/egress-proxy/values.local.yaml
# at the end. Those are the only things that leave this box.
#
# WHAT THIS BOX IS NOT. One VPS is ONE address. It is a second address, not a
# rotating one — which is exactly what smitele-bot needs (its Cloudflare
# clearance cookie is bound to the exit, so rotation fails within minutes) and
# what apartment-watch's geoip fingerprinting wants. See README.md for the three
# ways to get rotation when a service actually wants it.

set -euo pipefail

PROXY_PORT="${PROXY_PORT:-3129}"
CLUSTER_USER="${CLUSTER_USER:-homecluster}"
# The name the certificate is issued for, and the name the cluster verifies
# against. Does not need to resolve publicly — the cluster reaches this box by
# whatever `host` you put in values.local.yaml and verifies this name from the
# pinned cert.
TLS_NAME="${TLS_NAME:-egress.local}"
# The DDNS name that tracks the house. The firewall follows this, so a changing
# home address does not lock the cluster out.
HOME_DDNS="${HOME_DDNS:-zachd.duckdns.org}"
SSH_PORT="${SSH_PORT:-22}"

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq squid-openssl apt-transport-https ca-certificates \
  nftables unattended-upgrades openssl dnsutils >/dev/null 2>&1 || \
  apt-get install -y -qq squid nftables unattended-upgrades openssl dnsutils >/dev/null
# squid-openssl where available; plain squid otherwise. Either is fine — the
# cluster side was verified working against a GnuTLS build, so this does not
# depend on which TLS library the distro chose.

# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------
# WHY THE HOP IS ENCRYPTED AT ALL. Without it, the proxy credential and every
# CONNECT target hostname cross the public internet in cleartext — the tunnelled
# payload is the client's own TLS and is safe either way, but "which sites the
# house scrapes, and the password to do it" is not nothing.
#
# This is invisible to the applications: only squid speaks to this box.
# Playwright/Camoufox cannot use an https:// proxy URL at all, so if the
# applications had to reach here directly this would not be an option.
install -d -m 0750 -o proxy -g proxy /etc/squid/tls
if [[ ! -f /etc/squid/tls/proxy.pem ]]; then
  echo "==> Generating a self-signed certificate for ${TLS_NAME} (10 years)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout /etc/squid/tls/proxy.key -out /etc/squid/tls/proxy.crt \
    -subj "/CN=${TLS_NAME}" -addext "subjectAltName=DNS:${TLS_NAME}" >/dev/null 2>&1
  cat /etc/squid/tls/proxy.crt /etc/squid/tls/proxy.key > /etc/squid/tls/proxy.pem
  chown proxy:proxy /etc/squid/tls/proxy.*
  chmod 600 /etc/squid/tls/proxy.pem /etc/squid/tls/proxy.key
else
  echo "==> Certificate already present, keeping it"
  echo "    (delete /etc/squid/tls/proxy.* to reissue — the cluster's pinned"
  echo "     copy in values.local.yaml must be updated to match)"
fi

# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------
install -d -m 0750 -o proxy -g proxy /etc/squid/secret
if [[ ! -f /etc/squid/secret/cluster_password ]]; then
  openssl rand -base64 24 | tr -d '\n' > /etc/squid/secret/cluster_password
  chmod 600 /etc/squid/secret/cluster_password
fi
CLUSTER_PASS="$(cat /etc/squid/secret/cluster_password)"
printf '%s:%s\n' "$CLUSTER_USER" "$(openssl passwd -apr1 "$CLUSTER_PASS")" \
  > /etc/squid/secret/passwd
chown proxy:proxy /etc/squid/secret/passwd
chmod 640 /etc/squid/secret/passwd

# ---------------------------------------------------------------------------
# squid
# ---------------------------------------------------------------------------
echo "==> Writing /etc/squid/squid.conf"
sed -e "s|@PROXY_PORT@|${PROXY_PORT}|g" "${HERE}/squid.conf.template" > /etc/squid/squid.conf

if ! squid -k parse -f /etc/squid/squid.conf 2>&1 | grep -viE 'requires the use of Via' | grep -iqE 'error|fatal|aborting'; then
  echo "    config parses"
else
  echo "FAIL: squid rejected the config:"
  squid -k parse -f /etc/squid/squid.conf 2>&1 | tail -20
  exit 1
fi

systemctl enable squid >/dev/null 2>&1 || true
systemctl restart squid
sleep 2
systemctl is-active --quiet squid && echo "    squid running" || {
  echo "FAIL: squid did not start"; journalctl -u squid -n 30 --no-pager; exit 1; }

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------
# DEFENCE IN DEPTH, NOT THE CONTROL. Proxy auth is what actually keeps this from
# being an open relay. The firewall is here so that a stolen credential is not
# usable from anywhere on the internet — and it is built to fail CLOSED: if the
# DDNS lookup breaks, the set simply stops being refreshed and the cluster
# cannot connect. It never widens on failure.
echo "==> Firewall (nftables)"
install -m 0755 "${HERE}/duckdns-allow.sh" /usr/local/sbin/egress-allow-home
sed -e "s|@PROXY_PORT@|${PROXY_PORT}|g" -e "s|@SSH_PORT@|${SSH_PORT}|g" \
  "${HERE}/nftables.conf" > /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1 || true
nft -f /etc/nftables.conf
echo "    ruleset loaded"

cat > /etc/systemd/system/egress-allow-home.service <<EOF
[Unit]
Description=Point the egress proxy's firewall allowlist at the current home address
After=nftables.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=HOME_DDNS=${HOME_DDNS}
ExecStart=/usr/local/sbin/egress-allow-home
EOF

cat > /etc/systemd/system/egress-allow-home.timer <<'EOF'
[Unit]
Description=Refresh the egress proxy's home-address allowlist

[Timer]
# A residential address does not change often, but when it does the cluster is
# locked out until this runs — so it runs often. The cost is one DNS lookup.
OnBootSec=30s
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now egress-allow-home.timer >/dev/null 2>&1
HOME_DDNS="$HOME_DDNS" /usr/local/sbin/egress-allow-home || {
  echo "    WARN: could not resolve ${HOME_DDNS} yet — the timer will retry"; }

echo "==> Unattended security upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
echo
echo "============================================================"
echo " Paste into infra/egress-proxy/values.local.yaml"
echo "============================================================"
cat <<EOF

lanes:
  vps:
    enabled: true
    caCert: |
$(sed 's/^/      /' /etc/squid/tls/proxy.crt)
    peers:
      - name: vps
        host: "$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null || echo '<this-vps-public-ip>')"
        port: ${PROXY_PORT}
        tlsName: "${TLS_NAME}"

peerLogins:
  vps:
    username: "${CLUSTER_USER}"
    password: "${CLUSTER_PASS}"

EOF
echo "============================================================"
echo "Then: ./infra/egress-proxy/upgrade.sh"
echo
echo "Move a service onto this exit by setting its lane to 'vps' in"
echo "infra/egress-proxy/values.yaml. Nothing moves on its own."
