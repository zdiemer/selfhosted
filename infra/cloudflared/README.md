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

| Hostname | Service (origin) | TLS |
|---|---|---|
| `auth.diemer.codes` | `https://traefik.kube-system.svc.cluster.local:443` | No TLS Verify **on** |
| `webdav.diemer.codes` | same | same |
| `keepass.diemer.codes` | same | same |
| `docs.diemer.codes` | same | same |
| `pdf.diemer.codes` | same | same |
| `games.diemer.codes` | same | same |
| `romm.diemer.codes` | same | same |
| `talaria.deals` | same | same |

Leave the HTTP `Host` header blank (preserve original) so Traefik can match the
ingress rule. Adding each hostname auto-creates its proxied CNAME in that
hostname's own DNS zone — no manual DNS records. One tunnel serves several
zones: `talaria.deals` is an apex, not a `diemer.codes` subdomain.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml   # then paste the tunnel token
./upgrade.sh                                      # installs into the `infra` namespace
```

Health: `kubectl -n infra logs deploy/cloudflared` should show four
`Registered tunnel connection` lines. The connector also serves `/ready` on
`:2000` (used by the liveness/readiness probes).

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
