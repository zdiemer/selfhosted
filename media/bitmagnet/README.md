# bitmagnet — a tracker that is not a website

Every other indexer in [`media/arr`](../arr/) is someone else's website that we
scrape. That is the failure mode of the whole category: sites move domains, sit
behind Cloudflare, block datacenter and VPN address ranges, or simply die. This
one is different in kind — bitmagnet joins the BitTorrent DHT as a node, listens
to what the swarm is already announcing, and builds its own index in Postgres.
There is no site to be blocked from, no account, no rate limit, and no
FlareSolverr in the path.

```
DHT (public swarm) → bitmagnet dht_crawler → Postgres
                              ↓ queue_server classifies against TMDB
                     bitmagnet http_server → /torznab → Prowlarr → Sonarr/Radarr
```

## What it is good at, and what it is not

It is *wide*: it sees everything the DHT sees, including content no public
tracker lists. It is not *curated*: names are raw, quality metadata is thin,
there are no scene-group guarantees and no seeder-vetted "verified" flag. Treat
it as the fallback behind 1337x/TPB/YTS rather than the first stop — in Prowlarr
terms, a lower priority (its indexer priority is 25 by default; raise the
number to demote it further).

It is also **cold on arrival**. The index starts empty and fills at a few
thousand torrents an hour, so the first day returns almost nothing and the first
useful week is a week away. That is normal and not a misconfiguration.

## Shape

| Component | What it does |
|---|---|
| `bitmagnet` | One pod running all three workers: `http_server` (UI + Torznab), `queue_server` (TMDB classification), `dht_crawler`. Entirely stateless — no PVC. |
| `bitmagnet-postgres` | The index. Its own instance, not shared. |

UI: https://bitmagnet.zachd.duckdns.org, behind Authelia forward-auth.
In-cluster: `bitmagnet.media.svc:3333`; Torznab at
`http://bitmagnet.media.svc.cluster.local:3333/torznab`, **no API key**.

Because bitmagnet has no login of its own, the forward-auth middleware is the
only thing in front of a UI that can delete the whole index. Do not disable it
while the ingress is on.

## Storage and backup

The Postgres PVC carries `k8up.io/backup: "false"`. It is a cache of the public
DHT: nothing in it is authored here, it grows into the tens of GB, and rebuilding
it by crawling again is both cheaper and fresher than restoring it. The only
thing a rebuild loses is crawl history, which nothing depends on.

50Gi to start. `truenas-iscsi`, not NFS — Postgres wants real fsync.

## Egress

Deliberately outside both of the cluster's egress arrangements:

- **Not** through `infra/egress-proxy`, because a forward proxy cannot carry
  DHT's UDP at all.
- **Not** through the PIA pod in `media/arr`. That tunnel exists for
  qBittorrent's *transfers* and has one forwarded port; a DHT crawler on it
  would fight qBittorrent for the same connection table to no benefit. What
  bitmagnet moves is metadata — infohashes and names — not payload. Grabs still
  happen in qBittorrent, inside the tunnel, exactly as before.

The crawler's UDP port (3334) is not forwarded at the router. Crawling works
without it because bitmagnet initiates the queries; inbound reachability would
raise the discovery rate, not enable it.

## Prowlarr wiring

Added as a **Generic Torznab** indexer pointing at the in-cluster URL above with
an empty API key. Prowlarr's *Test* fails with "no results were returned from
your indexer" while the index is still empty — that is the cold-start above, not
a broken endpoint. Verify the endpoint directly instead:

```bash
kubectl -n media exec deploy/bitmagnet -- \
  wget -qO- 'http://localhost:3333/torznab/api?t=caps'
```

## Is it actually crawling?

```bash
kubectl -n media exec deploy/bitmagnet-postgres -- \
  psql -U bitmagnet -d bitmagnet -c 'select count(*) from torrents;'
```

That number should climb every time you run it. If it does not, check
`/status` — it reports the health of `dht`, `postgres` and `tmdb` separately:

```bash
kubectl -n media exec deploy/bitmagnet -- wget -qO- http://localhost:3333/status
```

## Two restarts on a fresh install are expected

bitmagnet exits 2 when Postgres is not answering yet, so the very first rollout
crash-loops for a few seconds until the database finishes `initdb`. It settles
on its own; only a restart count that keeps climbing means something is wrong.

## TMDB

The classifier uses a bundled default TMDB key that is rate-limited to one
request per second, which is why the log warns about it on every start. It is
enough for a homelab crawl. If classification falls behind, put a personal key
in `values.local.yaml` as `bitmagnet.tmdbApiKey`.
