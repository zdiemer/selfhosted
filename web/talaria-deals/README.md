# talaria-deals — talaria.deals front door

One Ingress. It publishes the sibling [talaria](../../../talaria) project at
`https://talaria.deals` through the shared Cloudflare tunnel.

```
browser ──TLS──▶ Cloudflare edge ──tunnel──▶ cloudflared (infra/cloudflared)
                                                   │ https, No TLS Verify
                                                   ▼
                             traefik.kube-system.svc.cluster.local:443
                                                   │ Host: talaria.deals
                                                   ▼
                                       talaria-nginx.default:80
```

## Why this is a chart of its own

Every other service publishes an external hostname by adding it to its own
`ingress.cloudflareHosts` list — see [infra/cloudflared](../../infra/cloudflared/).
talaria is the one service on the cluster whose chart lives in **another repo**,
so following that pattern would mean putting cluster routing config in the app
repo. This keeps it here instead.

That's a deliberate trade, and the cost is real: **this chart points at a Service
it doesn't own.** Nothing in the talaria repo knows this Ingress exists, so if
talaria renames `talaria-nginx` or moves off port 80, this route breaks and that
repo gets no warning. `upgrade.sh` pre-flights both, so the failure lands at
deploy time instead of silently 503-ing in production — but if you're working in
the talaria repo and touch the nginx Service, come edit `target` here.

It's a separate Ingress object rather than a rule added to talaria's, because
that one belongs to the `talaria` release; writing to it from here would leave
two releases fighting over one object.

## Additive, not a move

talaria still answers on `zachd.duckdns.org` through its own DuckDNS ingress,
which this chart doesn't touch. The DuckDNS name is still canonical *inside* the
app — it's what the CSP allows (`frontend/src/proxy.ts`), what email links point
at (`src/core/email/sender.py`), and what eBay's webhook is registered against.

Making `talaria.deals` canonical is a separate job, in the talaria repo, and the
eBay part has a sharp edge: `webhook.py` answers eBay's challenge by hashing the
endpoint URL together with the verification token, so the URL registered in
eBay's developer portal and the one in `secrets.prod.yaml` have to change in
lockstep or validation fails.

## No certificate here, on purpose

There's no `certresolver` annotation and no `tls:` host list, which looks like an
oversight and isn't:

- Public TLS for `talaria.deals` terminates at **Cloudflare's edge**. The tunnel
  dials Traefik with *No TLS Verify* on, so the cert Traefik presents is never
  checked — the tunnel is the trust boundary. Traefik answers with its default
  self-signed cert and the name mismatch is expected.
- Asking the `duckdns` resolver for a `talaria.deals` cert would fail anyway:
  DNS-01 can only be answered for the duckdns.org zone.
- Worse, listing `talaria.deals` under `tls:` would fold it into the DuckDNS cert
  **order**, and a failing domain can take the whole order — including the
  wildcard — down with it. Same reasoning as every `*.diemer.codes` host.

## Deploy

```bash
./upgrade.sh    # installs into `default`, beside talaria
```

The release lives in `default` rather than a namespace of its own: an Ingress can
only reference a Service in its own namespace, and talaria lives there.

`upgrade.sh` verifies the target Service exists on the expected port, then proves
Traefik routes the Host header by probing it in-cluster — so you get a real
answer without waiting on Cloudflare.

## The other half is in the dashboard

This chart only teaches Traefik to route the Host header. The
public-hostname → origin map lives **on the tunnel in Cloudflare**, not in git
(the connector pulls it down using its token). Until `talaria.deals` is added
there, this Ingress is unreachable. Zero Trust → Networks → Tunnels → the shared
tunnel → Public Hostnames:

| Field | Value |
|---|---|
| Hostname | `talaria.deals` |
| Service | `https://traefik.kube-system.svc.cluster.local:443` |
| TLS | No TLS Verify **on** |
| HTTP Host header | blank (preserve original) |

Adding it auto-creates the proxied DNS record in the zone — no manual record.

Verify:

```bash
curl -sI https://talaria.deals            # expect 200 from talaria's frontend
dig +short talaria.deals                  # expect Cloudflare's proxy IPs
```
