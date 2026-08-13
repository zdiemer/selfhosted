# The VPS exit

The far end of the `vps` lane. A ~$5/month box running squid, so that traffic
which should not come from the house doesn't.

```
pod --http--> [cluster squid] --WireGuard (tailnet)--> [this box] --> the internet
```

Everything here is run **once, on the VPS**. Nothing in this directory runs in
the cluster.

The hop rides the tailnet rather than TLS, and the public proxy port is closed
entirely — see [Security posture](#security-posture) for why that ended up being
both simpler and stronger than the TLS design it replaced.

## One address, or a rotating pool?

**A single VPS is one static address. It is a second address, not a rotating
one** — and for the service this was built for, that is the requirement rather
than a limitation. smitele-bot's Cloudflare clearance cookie is bound to the
exit that solved the challenge, there are twelve solves a day before a four-hour
breaker arms, and a cookie replayed from a different address is refused. A
rotating pool there does not work worse; it fails within minutes.
apartment-watch has the same preference for a different reason: Camoufox's
`geoip=True` derives locale, timezone and lat/long from the exit, so a stable,
plausible location is worth more than variety.

When a service genuinely wants rotation — because it is rate-limited *per
address* rather than challenged — there are three ways to get it, and all of
them stay flat-rate, which the bandwidth maths demands:

**1. More addresses on this same box — CHECK YOUR PROVIDER FIRST.** squid picks
the source per request with `tcp_outgoing_address`, either randomly or pinned per
client:

```squid
acl svc_scraper proxy_auth scraper
tcp_outgoing_address 203.0.113.11 svc_scraper     # this client always exits here
```

The commented block at the bottom of `squid.conf.template` has both shapes. But
this only works if the box actually has several public IPv4 addresses bound to
it, and **that is a provider question with genuinely different answers**:

| Provider | Extra IPv4 per instance? |
|---|---|
| Vultr, Hetzner, Linode | Yes, a few dollars a month each. `tcp_outgoing_address` works as written. |
| **DigitalOcean** | **No.** One public IPv4 per Droplet, and at most one Reserved IP assigned at a time. This approach is not available. |

On DigitalOcean a [Reserved IP](https://docs.digitalocean.com/products/networking/reserved-ips/)
can carry outbound traffic, but only by [repointing the Droplet's default
gateway](https://docs.digitalocean.com/products/networking/reserved-ips/how-to/outbound-traffic/)
at the anchor gateway — which moves *all* egress to that one address. That makes
it a fast way to **replace** an address (reassign in seconds, no rebuild), not a
way to rotate between several. Worth knowing that repointing the default gateway
on a remote box will drop your SSH session if you get it wrong.

DigitalOcean's IPv6 allocation is 16 addresses, which `tcp_outgoing_address`
could rotate across — but many scraping targets publish no AAAA record at all,
so the practical reach is small. BYOIP (bring your own /24) exists and is GA,
which is the real answer if you ever want many addresses there, and is far more
than a home cluster needs.

**2. More boxes, round-robin.** Run `bootstrap.sh` on a second and third VPS and
add them as peers of one lane with `roundRobin: true`. Squid round-robins across
peers, so the exit changes per connection. The `residential` lane in
`values.yaml` is already shaped for exactly this.

**3. A commercial pool.** A Webshare-style endpoint list drops straight into a
round-robin lane. BrightData does **not**: its rotation encodes country and
session in the *username*, per request, and a `cache_peer` carries one fixed
`login=`, so that provider has to stay app-side. Both bill per gigabyte, which
`discord/smitele-bot/docs/proxy-setup.md` rules out for a workload that moves
~118 GB/month.

Rotation is chosen **per lane, per service**, precisely because it is not a free
upgrade — turning it on globally would break smitele-bot.

**Replacing an address is a separate thing from rotating.** If this box's IP
gets banned, most providers let you detach and reattach a floating IP or rebuild
the instance for a new one. That is a manual, minutes-long fix, and it is why
the cluster names a *lane* rather than a host: swapping the address is a
`values.local.yaml` edit and one `upgrade.sh`.

## What to buy

| Requirement | Why |
|---|---|
| **Flat rate, ≥1 TB/month** | smitele-bot alone is ~118 GB/month steady plus ~36 GB per backfill. Per-gigabyte billing is the wrong shape entirely. |
| **Static IPv4** | The clearance cookie is bound to it. An address that changes on reboot defeats the point. |
| **US, ideally West Coast** | apartment-watch's Camoufox derives its fingerprint's location from the exit. An exit far from San Francisco is a *louder* signal than the current residential address. |
| Datacenter ASN is fine to start | What carries smitele-bot past Cloudflare is the solved cookie and the Firefox handshake, not the address's reputation. Escalate to a static ISP proxy only if this gets challenged — it is a config change, not a rewrite. |

## Install

**1. Join the tailnet first.** The default firewall posture admits the proxy port
*only* on `tailscale0`, so a box that is not on the tailnet is unreachable by
design. `--accept-dns=false` is deliberate: MagicDNS would repoint
`/etc/resolv.conf` at `100.100.100.100`, and this box resolves thousands of
scraping destinations — its resolver should not depend on tailscaled.

```sh
ssh root@<vps>
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --hostname=egress-sfo3 --accept-dns=false
tailscale ip -4          # this is the address the cluster will use
```

**2. Bootstrap.**

```sh
scp -r infra/egress-proxy/vps/ root@<vps>:/tmp/egress-vps
ssh root@<vps> 'bash /tmp/egress-vps/bootstrap.sh'
```

Tunable by environment: `PROXY_PORT` (3129), `CLUSTER_USER` (homecluster),
`HOME_DDNS` (zachd.duckdns.org), `SSH_PORT` (22), `TLS_NAME` (egress.local),
plus the ones that decide the posture:

| | Default | Meaning |
|---|---|---|
| `PUBLIC_FALLBACK` | `false` | Also admit the proxy port from the house over the public internet. Only needed for a box that is *not* on a tailnet. |
| `TLS_HOP` | `false` | Serve `https_port` instead of `http_port`. Only useful if the cluster side terminates TLS itself — squid's own `cache_peer tls` does not work against this box. |
| `CROWDSEC` | `true` | Run the CrowdSec agent **and the nftables firewall bouncer**, so detections actually block. Reads sshd, Caddy, haproxy and squid. Set `false` only to take enforcement off the box entirely. |
| `CROWDSEC_WHITELIST` | `100.64.0.0/10 192.168.4.0/24` | Networks CrowdSec may never ban — the tailnet (your recovery path) and the house LAN (the cluster's proxy traffic). Whitelisted at the parser stage, so they never accumulate toward a decision. |

⚠️ **`CROWDSEC=true` is the default, and it enforces.** That is deliberate — the
lanes on this box are the only public surfaces in the estate that Cloudflare and
Traefik never see — but it does mean a first run installs a thing that can ban
addresses. Check `cscli decisions list` after a day; the whitelist covers the
paths you would actually notice losing.

Idempotent — re-running keeps the existing certificate and password. It prints a
block ready to paste into `infra/egress-proxy/values.local.yaml`.

**3. Point the lane at the tailnet address, not the public one.** The printed
block carries the box's public IP, because that is what it can see about itself.
Replace it:

```yaml
    peers:
      - name: vps
        host: "100.x.y.z"     # tailscale ip -4, NOT the public address
```

**4. Apply.**

```sh
./infra/egress-proxy/upgrade.sh
```

That is the whole handover. **Nothing moves onto this exit on its own** — a
service switches when you change its `lane` to `vps` in `values.yaml`.

## The Minecraft relay (`MC_RELAY=true`)

A second, unrelated job this box can do: forward public `:25565` to the
cluster's Minecraft server over the tailnet, so friends keep playing after the
home router stops forwarding ports.

```
player --TCP 25565--> minecraft.diemer.codes (this box) --haproxy--> tailnet --> any cluster node --> mc pod
```

`minecraft.diemer.codes` is a **DNS-only (gray-cloud) A record** at the VPS
public IP — Cloudflare's free plan proxies HTTP only, not raw game TCP, so the
name must resolve straight to the box. haproxy (`haproxy-minecraft.cfg.template`)
listens on 25565 and forwards to every cluster node's tailnet address; the
`mc-minecraft` Service is `externalTrafficPolicy: Cluster`, so whichever node
receives the connection routes it on to the pod wherever it currently runs.

Enable it by re-running bootstrap with the flag:

```sh
scp -r infra/egress-proxy/vps/ root@<vps>:/tmp/egress-vps
ssh root@<vps> 'MC_RELAY=true bash /tmp/egress-vps/bootstrap.sh'
```

**No health checks on the backends, on purpose.** The server autopauses its JVM
when idle and wakes on any TCP connection — a periodic health probe looks
exactly like a player joining, so `check` would keep it awake forever and undo
the autopause. The cost is that haproxy only learns a node is dead when a real
connection to it fails: `retries 3` + `option redispatch` then move that one
join to another node, a few seconds of delay in the rare node-down case. The
node list is static (tailnet IPs don't churn); if the cluster gains or loses a
node, edit the template and re-run bootstrap.

This is orthogonal to the proxy lane above — a box can run either, both, or
neither. The relay port is a plain `tcp dport 25565 accept` (game clients can't
authenticate to a firewall); the Minecraft server's own whitelist is the access
control.

## Security posture

- **Proxy auth is the control.** An open proxy on a public IP is found by
  scanners within hours and used to attack other people from an address that
  bills to you. `http_access deny all` is the last line in the config and
  `authenticated` is the only thing that passes it.
- **The firewall is defence in depth**, so a stolen credential is not usable
  from anywhere on the internet. It follows the DuckDNS name the house already
  publishes, refreshed every 5 minutes, because a residential address changes
  without warning.
- **It fails closed.** The allowlist set starts empty and is only ever replaced
  wholesale from a successful lookup. If DNS breaks, the set stops being
  refreshed and the cluster loses its exit — loud, and safe. It never widens on
  failure, which would be worse than having no firewall because it would still
  look like one.
- **SSH stays open to the world, deliberately — but metered.** Locking it to the
  same DDNS set means one failed lookup plus a changed home address locks you
  out of a box with no console. Use keys, disable password auth. On top of that
  the public port is rate-limited to 10 new connections/minute per source
  (burst 5), which does nothing against a targeted attacker and a great deal
  against the thousands of daily credential-stuffing attempts that otherwise
  bury every real signal in the journal. **The tailnet path is exempt and listed
  first** — Tailscale SSH answers on the same port over `tailscale0`, and
  rate-limiting your own way back in is how a hardening change becomes an
  incident.
- **Every public lane has a kernel-level rate limit.** `nftables.conf` meters
  new connections per source on :22, :25565 and :80/:443 before anything
  userspace sees them. Sized per lane and deliberately generous — 30/min for
  Minecraft (client server-list pings are bursty), 240/min for the media lane
  (one Jellyfin stream opens many parallel connections) — because the job is to
  stop a flood, not to shape normal use. These are blind to behaviour: they
  count packets, not intent. CrowdSec is the layer that reads intent.
- **CrowdSec runs here, and it *enforces*.** Unlike `infra/crowdsec` in the
  cluster (detection-only, because its enforcement point was a Traefik plugin
  that fetches from a third party at pod start), this box runs the nftables
  firewall bouncer: a local daemon writing local nftables sets, with nothing to
  fetch and nothing to take down but itself. One agent reads sshd, Caddy,
  haproxy and squid, so every public listener is covered at once.

  This matters more here than anywhere else in the estate, because **everything
  on this box bypasses Cloudflare and Traefik entirely** — none of it reaches
  the cluster's WAF, its rate limiter, or its access log. What is not read here
  is not seen anywhere.

  The tailnet and the house LAN are whitelisted at the *parser* stage
  (`CROWDSEC_WHITELIST`), so they cannot accumulate toward a decision at all.
  That is the recovery path; it must not be bannable.
- **The Minecraft relay limits connections, not just packets.** haproxy holds a
  stick-table keyed on source: 8 concurrent and 30/minute. `maxconn 512` is
  global, so without it one source can take every slot and lock out real players
  — a denial of service that never reaches the Minecraft whitelist, because the
  whitelist lives on the far side of a connection that was never established.
- **The box can only reach two ports on the tailnet.** The input chain was
  always careful about who may reach this box; nothing was careful about what
  this box may reach, and it shares a tailnet with ten cluster nodes, the NAS
  and every personal device. It is simultaneously the most exposed machine here
  — public sshd, public HTTPS, public game port — and, until now, one with
  unrestricted L3 access to all of them. Tailscale SSH's own auth is a real
  control for *logging in* to a node; it does nothing about this box opening
  `:6443` on the API server or `:445` on the NAS.

  The output chain now permits only `25565` (haproxy → the Minecraft pod) and
  `30096` (Caddy → the jellyfin NodePort) toward `tailscale0`, plus ICMP and
  established flows. Everything else into the tailnet is dropped; outbound to
  the internet stays open, because being a forward proxy is the job.

  Verified after applying: both relay ports reachable, and `:6443`/`:22` on a
  node and `:445`/`:80` on the NAS all blocked. Applied behind a
  `systemd-run --on-active=300` auto-revert so a mistake would have restored
  itself, since inbound Tailscale SSH is answered *from* this chain and getting
  it wrong locks you out of the box the rule protects.
- **The firewall no longer flushes the whole ruleset.** `nftables.conf` resets
  only `table inet egress`. It used to open with `flush ruleset`, which was
  harmless with one firewall on the box and became a real bug the moment
  CrowdSec's bouncer started maintaining its own tables: every
  `systemctl restart nftables`, every re-run of `bootstrap.sh` and every reboot
  would have silently emptied the live ban set while still reporting healthy.
- **The rendered ruleset is checked before it is loaded.** `nft -c -f` parses it
  while the previous ruleset is still in the kernel, and a rejected config never
  becomes `/etc/nftables.conf`. haproxy and Caddy configs were already validated
  this way; the firewall — the one config here whose failure mode is "no
  filtering at all, on a public IP" — was not.
- **The hop rides the tailnet, not TLS.** squid *can* speak TLS to a cache_peer
  and it works between two identical builds, but the cluster image is
  `--with-gnutls` while Ubuntu's squid is OpenSSL, and across that pair this box
  accepts the CONNECT and the tunnel then carries no data. (Proven not to be
  this box's fault: `curl --proxy https://` through it works perfectly.) So the
  listener is plain and the hop is wrapped by WireGuard instead — which is
  strictly better, because it encrypts the whole conversation rather than just
  the proxy control channel, needs no certificate pinning, and lets the public
  listener be closed entirely.
- **With `PUBLIC_FALLBACK=false` there is no public listener at all.** That is
  what makes the proxy credential uninteresting: there is nothing on the
  internet to replay it against. It also removes the "residential address
  changed and locked the cluster out" failure mode, and makes the
  DuckDNS-following timer vestigial — it still refreshes the sets, but no rule
  consults them. Left running because it costs nothing and is the fallback if
  this box ever leaves the tailnet.
- **No destination allowlist here.** This box sees one authenticated client and
  has already lost the context of which service made the request. Filtering
  belongs on the cluster side, where the client authenticates by name.
- **`PUBLIC_FALLBACK` must stay `false` once DuckDNS is tailnet-only.** When
  `infra/duckdns` runs in `tailnet` mode, `zachd.duckdns.org` resolves to a
  `100.x` address, so `egress-allow-home` fills `home_v4` with a tailnet IP. No
  rule consults the set while the fallback is off, so this is harmless — but
  turning the fallback back on would open the proxy port to a CGNAT range, not
  the house. If this box ever needs a public proxy listener again, flip DuckDNS
  back to `wan-echo` first.

## Verify

From the cluster, after `upgrade.sh`:

```sh
# a client on the vps lane should report the VPS address, not the house
kubectl -n infra run t --rm -it --restart=Never --image=curlimages/curl:8.11.1 \
  --labels=egress.zachd/proxied=true -- \
  curl -s -x "http://<user>:<pass>@egress-proxy.infra.svc.cluster.local:3128" https://api.ipify.org
```

On the box:

```sh
systemctl status squid nftables egress-allow-home.timer
nft list set inet egress home_v4          # should hold the current home address
tail -f /var/log/squid/access.log         # same logfmt shape as the cluster side
```

CrowdSec, on the box. The one that matters is `cscli bouncers list`: an agent
with no registered bouncer makes decisions and enforces none of them, which
looks exactly like a healthy install.

```sh
cscli bouncers list        # MUST be non-empty, or nothing is being blocked
cscli metrics              # acquisition: every source should show lines read
cscli decisions list       # live bans
cscli alerts list          # what fired, whether or not it was actioned

# the meters, and how much they have actually dropped
nft list ruleset | grep -A2 meter

# prove the whitelist works — this must return nothing
cscli decisions list -o json | grep -E '100\.(6[4-9]|[7-9][0-9]|1[0-2][0-9])\.'
```

If a rule misfires and bans something it shouldn't:

```sh
cscli decisions delete --ip <addr>     # one
cscli decisions delete --all           # panic button; bouncer clears within seconds
```

**The tests worth actually doing** are that a dead exit fails closed rather than
falling back to the house — the whole point of `never_direct` — and that the
public port really is shut:

```sh
# 1. dead exit must refuse, not fall back
ssh root@<vps> systemctl stop squid     # (or: systemctl stop tailscaled)
#    the same request must now FAIL, not return the home address
ssh root@<vps> systemctl start squid

# 2. from a cluster pod: the public address must time out, the tailnet one work
curl --max-time 12 -x http://user:pass@<public-ip>:3129  https://api.ipify.org   # expect timeout
curl --max-time 12 -x http://user:pass@100.x.y.z:3129    https://api.ipify.org   # expect the VPS IP
```

Both verified on the live box: stopping tailscaled produced a timeout rather
than the home address, and the public listener times out while the tailnet one
answers.

## Known gaps

- **Single point of failure.** One box, no failover. A lane with two peers and
  `roundRobin: true` would survive one dying, at the cost of a rotating exit —
  which smitele-bot cannot have. Accept the SPOF or run two and split the lanes.
- **Nothing ships this box's logs anywhere.** journald, `/var/log/squid` and
  `/var/log/caddy` only. The cluster-side access log is the one that is shipped
  and attributed by service; this one exists to answer "did the request reach
  the exit at all". CrowdSec now *reads* all of them locally, so they are acted
  on even though they are never shipped — but the corollary is that its alerts
  are only visible on the box (`cscli alerts list`). Nothing pages you.
- **CrowdSec has no Minecraft scenarios.** The haproxy log is acquired, so
  connection-level noise is visible and the stick-table rejections show up as a
  pattern, but nobody has written scenarios for the Minecraft protocol itself.
  Gameplay abuse is the server whitelist's problem, not this box's.
- **The rate limits are per source address.** They shape single-source abuse and
  do nothing about a distributed flood; that is the provider's DDoS protection,
  upstream of this box and not configurable from here.
- **The certificate is pinned, so reissuing it is a two-sided change.** Deleting
  `/etc/squid/tls/proxy.*` and re-running mints a new one, and the cluster keeps
  rejecting the peer until `caCert` in `values.local.yaml` is updated to match.
  Ten-year expiry to make that rare.
- **`bootstrap.sh` is not configuration management.** It is idempotent, but it
  does not converge drift — if you hand-edit `/etc/squid/squid.conf` on the box,
  re-running overwrites it, and nothing detects the difference in between.
