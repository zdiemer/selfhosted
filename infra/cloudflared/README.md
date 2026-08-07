# cloudflared — shared Cloudflare Tunnel connector

A reusable, outbound-only [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
connector that publishes the cluster's user-facing services on an external domain
without opening any inbound port or depending on dynamic DNS. The chart is
domain-agnostic — it just runs the connector and feeds it a token. It fronts
`diemer.codes` and `talaria.deals`; adding another domain is config, not code
(see [Reusing for another domain](#reusing-for-another-domain)).

`talaria.deals` is the proof of that: it was added to this connector with **no
redeploy and no change to this chart** — one dashboard hostname, and a Traefik
route in [web/talaria-deals](../../web/talaria-deals/).

Cloudflare terminates public TLS at its edge and forwards each request through the
tunnel to the in-cluster **Traefik** service, which routes by `Host` header to the
right chart.

```
browser ──TLS──▶ Cloudflare edge ──tunnel──▶ cloudflared (this chart)
                                                   │ https, No TLS Verify
                                                   ▼
                              traefik.kube-system.svc.cluster.local:443
                                                   │ routes by Host header
                                                   ▼
                    authelia / keepass / paperless / stirling / gamedex / romm
```

Minecraft services stay on DuckDNS. `kelsey.green` is a separate domain with its
own zone and its own connector (`web/kelsey-green`), unrelated to this one.

## Why the tunnel points at Traefik (not each service)

One connector, one token. The alternative — a cloudflared sidecar per chart —
means seven tunnels and seven tokens to rotate. Instead every public hostname on
the tunnel points at the same origin and lets Traefik do the host routing it
already does for the DuckDNS ingresses. Each service keeps its existing DuckDNS
ingress and *also* grows an `ingress.cloudflareHosts` list for its external
name(s) (TLS for those hosts is handled at Cloudflare, so they are intentionally
left out of the ACME cert list). Because it's a list, a service can be published
on several domains at once.

## Dashboard-side config (do this once, in Cloudflare)

The public-hostname → origin map lives **on the tunnel**, not in this chart. In
Zero Trust → Networks → Tunnels → *your tunnel* → **Public Hostnames**, add one
per service, all with the same origin:

Every entry uses the same origin —
`https://traefik.kube-system.svc.cluster.local:443`, **No TLS Verify on** —
so the only column that actually varies is the hostname:

| Hostname | Backing ingress | Service |
|---|---|---|
| `auth.diemer.codes` | `auth/authelia` | `authelia:9091` |
| `claude.diemer.codes` | `claude/claude-workspace` | `claude-workspace:7681` |
| `docs.diemer.codes` | `docs/paperless` | `paperless:8000` |
| `games.diemer.codes` | `games/gamedex` | `gamedex:8080` |
| `happy.diemer.codes` | `happy/happy-server` | `happy-server:3005` |
| `homes.diemer.codes` | `web/apartment-watch-web` | `apartment-watch-web:8080` |
| `keepass.diemer.codes` | `auth/keepass-keeweb` | `keepass-keeweb:80` |
| `old.diemer.codes` | `web/old-diemer-codes` | `old-diemer-codes:80` |
| `pdf.diemer.codes` | `docs/stirling` | `stirling:8080` |
| `romm.diemer.codes` | `games/romm` | `romm:8080` |
| `smite.diemer.codes` | `discord/smitele-bot-web` | `smitele-bot-web:8080` |
| `status.diemer.codes` | `infra/cluster-status` | `cluster-status:80` |
| `webdav.diemer.codes` | `auth/keepass-webdav` | `keepass-webdav:80` |
| `talaria.deals` | `default/talaria-deals` | `talaria-nginx:80` |

Leave the HTTP `Host` header blank (preserve original) so Traefik can match the
ingress rule. Adding each hostname auto-creates its proxied CNAME in that
hostname's own DNS zone — no manual DNS records. One tunnel serves several
zones: `talaria.deals` is an apex, not a `diemer.codes` subdomain.

### This table can drift, and the cluster is the honest half

The dashboard is authoritative for routing, and nothing in this repo can read
it. What the table above actually shows is the **cluster's side** — every
non-`duckdns` host some Ingress claims to serve. Regenerate it any time:

```bash
kubectl get ingress -A -o json | python3 -c "
import sys, json
for i in json.load(sys.stdin)['items']:
    ns, nm = i['metadata']['namespace'], i['metadata']['name']
    for r in i['spec'].get('rules', []):
        h = r.get('host', '')
        if h and not h.endswith('zachd.duckdns.org'):
            b = r['http']['paths'][0]['backend']['service']
            print(f\"{h:<28} {ns}/{nm:<24} {b['name']}\")
" | sort
```

Drift shows up in one of two shapes, and they fail very differently:

- **In the cluster, not in the dashboard** — the host 404s at Cloudflare's edge
  and never reaches the tunnel. Obvious the moment anyone tries it.
- **In the dashboard, not in the cluster** — the request arrives at Traefik with
  a `Host` header no ingress rule matches, and Traefik answers with its default
  backend (404) over the self-signed default cert. This one is easy to miss,
  because the tunnel itself looks perfectly healthy.

Since the whole point of a table like this is to catch the second case, treat a
row here with no matching ingress as a bug rather than a leftover.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml   # then paste the tunnel token
./upgrade.sh                                      # installs into the `infra` namespace
```

Health: `kubectl -n infra logs deploy/cloudflared` should show four
`Registered tunnel connection` lines. The connector also serves `/ready` on
`:2000` (used by the liveness/readiness probes).

## Known: episodic QUIC reconnects

Both tunnels (this one and `web/kelsey-green-cloudflared`) periodically drop and
re-dial their edge connections:

```
WRN Failed to dial a quic connection error="failed to dial to edge with quic: timeout: no recent network activity"
ERR failed to accept incoming stream requests error="failed to accept QUIC stream: Application error 0x0 (remote)"
INF Registered tunnel connection location=sjc06 protocol=quic
```

This is **episodic, not continuous** — roughly 100 such lines over three weeks,
arriving in tight bursts on a handful of days and nothing at all in between. The
shape (both tunnels at once, clustered, self-healing) points at the house's
uplink rather than at cloudflared or the cluster.

It is not currently worth chasing: four connections are maintained precisely so
a single one dropping is invisible, and requests only fail if a burst takes out
enough of them at once. Worth revisiting only if the access log
(`infra/traefik` → `infra/alloy`) shows `diemer.codes` 5xx lining up with these
bursts — which is now something that can actually be checked.

## Reusing for another domain

Nothing here is tied to `diemer.codes`. To publish a second domain, pick one:

- **Same tunnel (simplest).** A single tunnel can publish hostnames from any
  number of zones in the same Cloudflare account. Add the new domain to Cloudflare
  (Add a site → move nameservers), then add its public hostnames to *this* tunnel
  in the dashboard, pointing at the same Traefik origin. Add each new host to the
  relevant service's `ingress.cloudflareHosts` list and `helm upgrade` that
  service. This connector serves the new domain with **no redeploy of its own**.
- **Separate connector (isolation).** If you'd rather keep domains on independent
  tunnels/tokens, install a second release of this chart:

  ```bash
  RELEASE=cloudflared-otherdomain NAMESPACE=infra ./upgrade.sh
  ```

  Every resource is release-name-derived, so connectors coexist without collision.

## Rotating the token

Regenerate under Zero Trust → Tunnels → *tunnel* → Refresh token, update
`values.local.yaml`, re-run `./upgrade.sh`. The `checksum/secret` annotation
rolls the pods automatically when the token changes.
