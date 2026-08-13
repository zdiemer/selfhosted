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
# what apartment-watch's geoip fingerprinting wanted before it was deprecated.
# See README.md for the three
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
# Serve jellyfin.diemer.codes from this box (Caddy, real Let's Encrypt cert)
# and reverse-proxy it over the tailnet to the cluster's jellyfin-lan NodePort,
# so family video doesn't ride the Cloudflare tunnel (whose free tier restricts
# streaming). Opens 80/443. The upstreams and host live in the Caddyfile
# template; MEDIA_NODES lets you override the node list without editing it.
MEDIA_RELAY="${MEDIA_RELAY:-false}"
MEDIA_HOST="${MEDIA_HOST:-jellyfin.diemer.codes}"
ACME_EMAIL="${ACME_EMAIL:-zach.diemer@gmail.com}"
# Node Tailscale IPs, :30096 = the jellyfin-lan NodePort (etp=Cluster, so any
# node reaches the pod). Space-separated; Caddy load-balances with failover.
MEDIA_NODES="${MEDIA_NODES:-100.121.136.39:30096 100.84.179.82:30096 100.118.242.89:30096 100.124.40.81:30096 100.122.194.1:30096}"
# Run CrowdSec on this box, with the nftables firewall bouncer ENFORCING.
#
# This is the one control here that both detects and blocks, and it is the only
# one that covers every public listener at once: sshd, Caddy (the media lane)
# and haproxy (Minecraft) all write logs, and one agent reads all of them.
#
# WHY IT ENFORCES HERE WHEN THE CLUSTER'S CROWDSEC DOES NOT. infra/crowdsec runs
# detection-only because its natural enforcement point was a Traefik plugin that
# fetches from plugins.traefik.io at pod start, making every Traefik restart
# depend on a third party being up. There is no equivalent hazard on this box:
# the firewall bouncer is a local daemon writing local nftables sets, with
# nothing to fetch at boot and nothing to take down but itself.
#
# WHAT IT PROTECTS THAT NOTHING ELSE DOES. Everything on this VPS bypasses
# Cloudflare and Traefik entirely, so the cluster's WAF, rate limiter and
# CrowdSec never see any of it. Before this, the only controls on the public
# lanes were the nftables meters (blind to behaviour) and each app's own auth.
CROWDSEC="${CROWDSEC:-true}"
# Addresses CrowdSec must never ban, no matter what the logs say. The tailnet is
# how you get back in when a rule misfires, and the house is where the cluster
# talks from — banning either turns a detection into an outage. Space-separated.
CROWDSEC_WHITELIST="${CROWDSEC_WHITELIST:-100.64.0.0/10 192.168.4.0/24}"

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
# The relay lanes are each THREE rules now (two rate meters, then the accept),
# so they are no longer substituted as a whole rule. Instead each line in
# nftables.conf carries a `@MC_@` / `@MEDIA_@` prefix that renders to nothing
# when the lane is on and to `# ` when it is off.
#
# That inversion is what keeps the actual firewall rules readable in git, with
# their comments, instead of being assembled here out of a shell string. It
# also sidesteps a real trap: sed cannot substitute a multi-line replacement
# without escaping every newline, so the string-building version would have had
# to collapse three rules onto one `;`-separated line to work at all.
if [[ "$MC_RELAY" == "true" ]]; then MC_P=""; else MC_P="# "; fi
if [[ "$MEDIA_RELAY" == "true" ]]; then MEDIA_P=""; else MEDIA_P="# "; fi
sed -e "s|@PROXY_PORT@|${PROXY_PORT}|g" -e "s|@SSH_PORT@|${SSH_PORT}|g" \
    -e "s|@PUBLIC_FALLBACK_V4@|${FB4}|" -e "s|@PUBLIC_FALLBACK_V6@|${FB6}|" \
    -e "s|@MC_@|${MC_P}|g" -e "s|@MEDIA_@|${MEDIA_P}|g" \
  "${HERE}/nftables.conf" > /etc/nftables.conf.new

# CHECK BEFORE SWAPPING. haproxy gets `haproxy -c` and Caddy gets `caddy
# validate` a few sections down; the firewall — the one config on this box whose
# failure mode is "no filtering at all, on a public IP" — got neither, and
# rendered straight over the live file.
#
# `nft -c -f` parses without loading. It is the only step here that can catch a
# malformed rule while the previous ruleset is still the one in the kernel, and
# it is why the render above goes to a .new file: a rejected config never
# becomes /etc/nftables.conf, so a re-run after a bad edit still has a working
# file to fall back to and `systemctl restart nftables` at boot cannot load a
# broken one.
if ! nft -c -f /etc/nftables.conf.new; then
  echo "FAIL: nftables rejected the rendered ruleset — live ruleset left untouched"
  echo "      the rejected file is at /etc/nftables.conf.new for inspection"
  exit 1
fi
mv /etc/nftables.conf.new /etc/nftables.conf
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

# ---------------------------------------------------------------------------
# Media lane (Caddy)
# ---------------------------------------------------------------------------
if [[ "$MEDIA_RELAY" == "true" ]]; then
  echo "==> Media lane (Caddy on :443 → tailnet, host ${MEDIA_HOST})"
  if ! command -v caddy >/dev/null 2>&1; then
    # Official Caddy apt repo (Cloudsmith). Pinned key path so re-runs are idempotent.
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
  fi
  # The access log directory, owned by the user Caddy drops to. Caddy does not
  # create it and does not fail loudly when it cannot write there — it starts,
  # serves traffic, and logs nothing, which would take CrowdSec's caddy
  # acquisition down with it while everything reported healthy.
  install -d -o caddy -g caddy -m 0750 /var/log/caddy
  # Build the space-separated upstream list for the Caddyfile.
  sed -e "s|@ACME_EMAIL@|${ACME_EMAIL}|g" \
      -e "s|jellyfin.diemer.codes|${MEDIA_HOST}|g" \
      -e "s|@MEDIA_UPSTREAMS@|${MEDIA_NODES}|g" \
    "${HERE}/caddy-media.Caddyfile.template" > /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || {
    echo "FAIL: Caddy rejected the config"; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; exit 1; }
  # AFTER validate, and that ordering is the entire point.
  #
  # `caddy validate` runs as root here and does not merely parse — it
  # instantiates the config's log writer, which CREATES /var/log/caddy/access.log
  # owned by root:root mode 0600. The service then starts as the `caddy` user,
  # cannot open its own log file, and exits 1 with `permission denied`. The
  # install -d above is not enough: it fixes the directory, and validate then
  # creates a file inside it that the directory's ownership says nothing about.
  #
  # Found the hard way — this took the media lane down on the first run that
  # added an access log.
  chown -R caddy:caddy /var/log/caddy
  systemctl enable caddy >/dev/null 2>&1 || true
  systemctl restart caddy
  systemctl is-active --quiet caddy && echo "    caddy running" || {
    echo "FAIL: caddy did not start"; journalctl -u caddy -n 30 --no-pager; exit 1; }
  echo "    NOTE: ${MEDIA_HOST} must be a DNS-only (gray-cloud) A record → this box,"
  echo "          or the Let's Encrypt challenge and viewer traffic won't reach Caddy."
elif systemctl is-active --quiet caddy 2>/dev/null; then
  echo "==> MEDIA_RELAY=false but caddy is running — stopping it"
  systemctl disable --now caddy
fi

# ---------------------------------------------------------------------------
# CrowdSec — detection AND enforcement
# ---------------------------------------------------------------------------
if [[ "$CROWDSEC" == "true" ]]; then
  echo "==> CrowdSec (agent + nftables firewall bouncer)"
  if ! command -v cscli >/dev/null 2>&1; then
    # CrowdSec's own apt repo. Their install script is the documented path and
    # is what keeps the repo key and suite correct across Debian/Ubuntu.
    curl -s https://install.crowdsec.net | bash >/dev/null
    apt-get update -qq
    apt-get install -y -qq crowdsec >/dev/null
  fi

  # Acquisition. Written wholesale to its own file rather than appended to
  # /etc/crowdsec/acquis.yaml: the package owns that file and rewrites it on
  # upgrade, so an appended stanza is one `apt upgrade` away from vanishing.
  # Anything in acquis.d/ survives.
  mkdir -p /etc/crowdsec/acquis.d
  cp "${HERE}/crowdsec-acquis.yaml.template" /etc/crowdsec/acquis.d/egress-vps.yaml

  # The whitelist, from CROWDSEC_WHITELIST. Rendered as a YAML list, indented
  # under `cidr:` — four spaces, because this is a nested sequence and CrowdSec
  # rejects the file outright if the indentation is wrong.
  #
  # Built by truncating the template at the placeholder line and appending the
  # rendered list, rather than by substituting into it. sed replaces within ONE
  # line, so a multi-line replacement has to arrive pre-escaped — which is both
  # unreadable and the kind of quoting that breaks the first time a value has a
  # slash in it. Truncate-and-append has neither problem.
  mkdir -p /etc/crowdsec/parsers/s02-enrich
  {
    sed '/@CROWDSEC_WHITELIST_CIDRS@/,$d' "${HERE}/crowdsec-whitelist.yaml.template"
    for cidr in $CROWDSEC_WHITELIST; do printf '    - "%s"\n' "$cidr"; done
  } > /etc/crowdsec/parsers/s02-enrich/egress-vps-whitelist.yaml

  # Collections. sshd and caddy are the two that actually fire here; the
  # http-cve and base-http scenarios are what turn "someone requested
  # /wp-login.php" into a decision rather than a log line.
  #
  # `cscli collections install` is idempotent and quiet about already-installed
  # items, so this is safe on every re-run.
  #
  # `cscli setup` (run by the installer) already installs sshd, caddy, haproxy
  # and linux, so most of this list is a no-op confirming what is there. The two
  # that it does NOT infer from the running services are http-cve and
  # base-http-scenarios — generic HTTP attack patterns, which is precisely what
  # a public Caddy needs and what service detection cannot know to want.
  for c in crowdsecurity/sshd crowdsecurity/caddy crowdsecurity/haproxy \
           crowdsecurity/base-http-scenarios \
           crowdsecurity/http-cve crowdsecurity/whitelist-good-actors; do
    cscli collections install "$c" >/dev/null 2>&1 || echo "    WARN: could not install $c"
  done

  # The bouncer. THIS is what makes any of the above block rather than observe.
  if ! command -v cs-firewall-bouncer >/dev/null 2>&1; then
    apt-get install -y -qq crowdsec-firewall-bouncer-nftables >/dev/null
  fi
  # nftables mode, and it maintains its OWN tables (crowdsec / crowdsec6) rather
  # than touching `inet egress`. That separation is why nftables.conf had to stop
  # saying `flush ruleset` — see the note at the top of that file.
  systemctl enable crowdsec crowdsec-firewall-bouncer >/dev/null 2>&1 || true
  systemctl restart crowdsec
  systemctl restart crowdsec-firewall-bouncer

  systemctl is-active --quiet crowdsec || {
    echo "FAIL: crowdsec did not start"; journalctl -u crowdsec -n 30 --no-pager; exit 1; }
  systemctl is-active --quiet crowdsec-firewall-bouncer || {
    echo "FAIL: crowdsec-firewall-bouncer did not start"
    journalctl -u crowdsec-firewall-bouncer -n 30 --no-pager; exit 1; }

  # PROVE THE BOUNCER IS REGISTERED, don't assume it. A bouncer whose API key
  # never registered runs happily and blocks nothing — the exact failure this
  # whole section exists to avoid, wearing the uniform of a healthy service.
  if cscli bouncers list 2>/dev/null | grep -q .; then
    echo "    bouncer registered:"
    cscli bouncers list 2>/dev/null | sed 's/^/      /' | head -8
  else
    echo "    WARN: no bouncer registered — decisions will be made but NOT enforced"
  fi
  echo "    acquisition:"
  cscli metrics 2>/dev/null | grep -A6 -i "acquisition" | sed 's/^/      /' | head -10 || true
  echo "    whitelisted: ${CROWDSEC_WHITELIST}"
elif systemctl is-active --quiet crowdsec 2>/dev/null; then
  echo "==> CROWDSEC=false but crowdsec is running — stopping it"
  systemctl disable --now crowdsec crowdsec-firewall-bouncer || true
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
