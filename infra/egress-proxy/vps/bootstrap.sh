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
# Encrypt the cluster->VPS hop? Default off, and that is a finding rather than a
# preference: squid's cache_peer TLS works between two identical builds but the
# cluster's image is --with-gnutls and Ubuntu's squid is OpenSSL, and across
# that pair this box accepts the CONNECT and the tunnel then carries no data.
# Verified from the cluster that `curl --proxy https://` through here works, so
# the listener is not the problem — squid-to-squid tunnelling is.
#
# Set TLS_HOP=true only if the cluster side terminates TLS itself (an stunnel or
# ghostunnel sidecar), not by pointing squid's cache_peer at it directly.
TLS_HOP="${TLS_HOP:-false}"
# Accept the proxy port from the house over the public internet, as well as over
# the tailnet. Default false: once the cluster reaches this box on its 100.x
# address, a public listener is pure exposure, and closing it is what makes the
# cleartext proxy credential (see TLS_HOP above) worthless to an observer.
#
# Set true if this box is NOT on a tailnet. SSH is unaffected either way.
PUBLIC_FALLBACK="${PUBLIC_FALLBACK:-false}"
# Relay public :25565 over the tailnet to the cluster's Minecraft server, so
# friends keep playing after the home router stops forwarding ports. Renders
# haproxy-minecraft.cfg.template and opens the port in nftables. The backend
# node list lives in the template — edit it in git, re-run this script.
MC_RELAY="${MC_RELAY:-false}"

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq squid-openssl apt-transport-https ca-certificates \
  nftables unattended-upgrades openssl dnsutils curl >/dev/null 2>&1 || \
  apt-get install -y -qq squid nftables unattended-upgrades openssl dnsutils curl >/dev/null
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
echo "==> Writing /etc/squid/squid.conf (TLS hop: ${TLS_HOP})"
if [[ "$TLS_HOP" == "true" ]]; then
  LISTENER="https_port ${PROXY_PORT} tls-cert=/etc/squid/tls/proxy.pem"
else
  LISTENER="http_port ${PROXY_PORT}"
fi
sed -e "s|@PROXY_PORT@|${PROXY_PORT}|g" -e "s|@LISTENER@|${LISTENER}|g" \
  "${HERE}/squid.conf.template" > /etc/squid/squid.conf

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
if [[ "$PUBLIC_FALLBACK" == "true" ]]; then
  FB4="tcp dport ${PROXY_PORT} ip  saddr @home_v4 accept"
  FB6="tcp dport ${PROXY_PORT} ip6 saddr @home_v6 accept"
else
  FB4="# public fallback disabled (PUBLIC_FALLBACK=false) — tailnet only"
  FB6="# the home_v4/home_v6 sets are still refreshed, but nothing consults them"
fi
if [[ "$MC_RELAY" == "true" ]]; then
  MCR="tcp dport 25565 accept"
else
  MCR="# minecraft relay disabled (MC_RELAY=false)"
fi
sed -e "s|@PROXY_PORT@|${PROXY_PORT}|g" -e "s|@SSH_PORT@|${SSH_PORT}|g" \
    -e "s|@PUBLIC_FALLBACK_V4@|${FB4}|" -e "s|@PUBLIC_FALLBACK_V6@|${FB6}|" \
    -e "s|@MC_RELAY@|${MCR}|" \
  "${HERE}/nftables.conf" > /etc/nftables.conf
# Through the unit, not a bare `nft -f`. Both load the same file, but loading it
# directly leaves nftables.service reporting `inactive` on a box that is in fact
# firewalled — and `systemctl is-active nftables` is the obvious health check, so
# a correct box that looks broken is its own kind of problem.
systemctl enable nftables >/dev/null 2>&1 || true
systemctl restart nftables
systemctl is-active --quiet nftables && echo "    ruleset loaded (nftables.service active)" || {
  echo "FAIL: nftables did not start"; systemctl status nftables --no-pager -l | tail -20; exit 1; }

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

# ---------------------------------------------------------------------------
# Minecraft relay
# ---------------------------------------------------------------------------
if [[ "$MC_RELAY" == "true" ]]; then
  echo "==> Minecraft relay (haproxy on :25565 → tailnet)"
  apt-get install -y -qq haproxy >/dev/null
  # Wholesale, not appended: this box runs haproxy for exactly one job, and a
  # config assembled from fragments is how two jobs end up fighting later.
  cp "${HERE}/haproxy-minecraft.cfg.template" /etc/haproxy/haproxy.cfg
  haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null || {
    echo "FAIL: haproxy rejected the config"; haproxy -c -f /etc/haproxy/haproxy.cfg; exit 1; }
  systemctl enable haproxy >/dev/null 2>&1 || true
  systemctl restart haproxy
  systemctl is-active --quiet haproxy && echo "    haproxy running" || {
    echo "FAIL: haproxy did not start"; journalctl -u haproxy -n 30 --no-pager; exit 1; }
elif systemctl is-active --quiet haproxy 2>/dev/null; then
  echo "==> MC_RELAY=false but haproxy is running — stopping it"
  systemctl disable --now haproxy
fi

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
    tls: ${TLS_HOP}
$(if [[ "$TLS_HOP" == "true" ]]; then
    printf '    caCert: |\n'
    sed 's/^/      /' /etc/squid/tls/proxy.crt
  fi)
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
