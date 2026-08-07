# The VPS exit

The far end of the `vps` lane. A ~$5/month box running squid, so that traffic
which should not come from the house doesn't.

```
pod --http--> [cluster squid] --TLS--> [this box] --> the internet
```

Everything here is run **once, on the VPS**. Nothing in this directory runs in
the cluster.

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

**1. More addresses on this same box.** Most providers sell extra IPv4 for a few
dollars a month and hand out a `/64` of IPv6 for free. squid picks the source
per request with `tcp_outgoing_address`, either randomly or pinned per client:

```squid
acl svc_scraper proxy_auth scraper
tcp_outgoing_address 203.0.113.11 svc_scraper     # this client always exits here
```

The commented block at the bottom of `squid.conf.template` has both shapes. This
is the cheapest real rotation and it needs no second machine.

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

```sh
scp -r infra/egress-proxy/vps/ root@<vps>:/tmp/egress-vps
ssh root@<vps> 'bash /tmp/egress-vps/bootstrap.sh'
```

Tunable by environment: `PROXY_PORT` (3129), `CLUSTER_USER` (homecluster),
`TLS_NAME` (egress.local), `HOME_DDNS` (zachd.duckdns.org), `SSH_PORT` (22).

It is idempotent — re-running keeps the existing certificate and password. It
prints a block ready to paste into `infra/egress-proxy/values.local.yaml`, then:

```sh
./infra/egress-proxy/upgrade.sh
```

That is the whole handover. **Nothing moves onto this exit on its own** — a
service switches when you change its `lane` to `vps` in `values.yaml`.

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
- **SSH stays open to the world, deliberately.** Locking it to the same DDNS set
  means one failed lookup plus a changed home address locks you out of a box
  with no console. Use keys, disable password auth.
- **The hop is TLS.** Not for the payload — that is the client's own TLS and is
  safe regardless — but because the credential and every CONNECT target hostname
  would otherwise cross the internet in cleartext. This is only possible because
  *only squid* talks to this box: Playwright/Camoufox cannot use an `https://`
  proxy URL at all.
- **No destination allowlist here.** This box sees one authenticated client and
  has already lost the context of which service made the request. Filtering
  belongs on the cluster side, where the client authenticates by name.

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

**The test worth actually doing** is that a dead exit fails closed rather than
falling back to the house — that is the whole point of `never_direct`:

```sh
ssh root@<vps> systemctl stop squid
# the same curl must now FAIL, not return the home address
ssh root@<vps> systemctl start squid
```

This was verified against a two-squid chain before any VPS existed: with the
peer stopped, the request returned `503`, and the address never leaked.

## Known gaps

- **Single point of failure.** One box, no failover. A lane with two peers and
  `roundRobin: true` would survive one dying, at the cost of a rotating exit —
  which smitele-bot cannot have. Accept the SPOF or run two and split the lanes.
- **Nothing ships this box's logs anywhere.** journald and `/var/log/squid` only.
  The cluster-side access log is the one that is shipped and attributed by
  service; this one exists to answer "did the request reach the exit at all".
- **The certificate is pinned, so reissuing it is a two-sided change.** Deleting
  `/etc/squid/tls/proxy.*` and re-running mints a new one, and the cluster keeps
  rejecting the peer until `caCert` in `values.local.yaml` is updated to match.
  Ten-year expiry to make that rare.
- **`bootstrap.sh` is not configuration management.** It is idempotent, but it
  does not converge drift — if you hand-edit `/etc/squid/squid.conf` on the box,
  re-running overwrites it, and nothing detects the difference in between.
