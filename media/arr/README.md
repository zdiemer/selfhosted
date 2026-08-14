# arr — acquisition for the media library

Prowlarr + Sonarr + Radarr + qBittorrent-inside-gluetun, plus SABnzbd for
Usenet and FlareSolverr for the Cloudflare-gated indexers — the automation
behind [Jellyfin](../jellyfin/). The flow:

```
Jellyseerr request → Radarr/Sonarr → search via Prowlarr's indexers
  → (Cloudflare-gated ones proxied through FlareSolverr)
  → grab handed to whichever client fits the release:
      torrent → qBittorrent (all traffic inside the PIA tunnel)
      nzb     → SABnzbd (normal egress; nothing to hide from a paid provider)
  → completed download hardlinked into /media/movies|tv (instant, no copy)
  → Jellyfin rescan; Jellyseerr marks it available
```

## The VPN pod

qBittorrent never has its own network: gluetun is a sidecar in the same pod,
owns the routing table, and firewalls everything that isn't the tunnel.
Tunnel down = no route out (a real killswitch), never a quiet fallback to
the house IP. PIA port forwarding is on (`SERVER_REGIONS` must stay on
PF-capable regions — the CA ones are), and gluetun pushes the dynamic
forwarded port into qBittorrent's listen port over localhost whenever it
changes.

This pod deliberately bypasses `infra/egress-proxy` — the point is exiting
from PIA's address. One opaque VPN flow is its correct shape in the egress
inventory. Everything else in this chart (arr metadata lookups, indexer
queries) egresses normally and could be onboarded to the proxy later.

PIA credentials: `values.local.yaml` from `op://homelab/media-arr` — the
standard `op inject` contract.

## Storage

Every file-touching container mounts the same NFS export at `/media`
(`192.168.4.36:/mnt/vault/media`) — one filesystem, identical paths
everywhere, which is what makes imports hardlinks instead of copies that
double disk during seeding. Config volumes are per-app `truenas-iscsi` PVCs
(SQLite), inside k8up's default backup; `/media` is not a PVC and is never
backed up (re-acquirable by definition).

## Admin UIs

All behind Authelia forward-auth (stirling-pdf pattern), plus each app's own
login:

| App | URL | In-cluster |
|---|---|---|
| Prowlarr | https://prowlarr.zachd.duckdns.org | `arr-prowlarr.media.svc:9696` |
| Sonarr | https://sonarr.zachd.duckdns.org | `arr-sonarr.media.svc:8989` |
| Radarr | https://radarr.zachd.duckdns.org | `arr-radarr.media.svc:7878` |
| qBittorrent | https://qbit.zachd.duckdns.org | `arr-qbittorrent.media.svc:8080` |
| SABnzbd | https://sab.zachd.duckdns.org | `arr-sabnzbd.media.svc:8080` |

FlareSolverr has no UI and no ingress on purpose — it is reachable only at
`arr-flaresolverr.media.svc:8191`, from Prowlarr.

## Usenet

qBittorrent covers what someone is still seeding. That is most things, and
not the long tail: a 1990s season can sit at zero peers on every public
indexer at once, which is unfixable by adding more indexers because the
bytes are genuinely gone. Usenet retention does not depend on a live swarm,
so SABnzbd exists for the back catalogue rather than for day-to-day grabs.

Both clients stay enabled in Sonarr and Radarr simultaneously — a release is
either a torrent or an nzb, and each goes to the client that can fetch it.

Three accounts, none of them in this chart:

| Piece | What it is | Notes |
|---|---|---|
| Provider | The actual Usenet feed (Eweka, Newshosting, …) | A block account is the right shape here — this fills gaps, it does not run continuously |
| Indexer | NZBgeek, DrunkenSlug, NZBFinder, NZBPlanet | Prowlarr ships all four definitions; they need your API key |
| SABnzbd | Deployed by this chart | Provider server + categories set in its UI |

SABnzbd is **not** in the VPN pod, and that is deliberate: it is an
authenticated TLS session to a provider billing your name. There is no swarm
to hide from, the provider already knows exactly who you are, and routing it
through PIA would only cost throughput.

## First-run wiring (once, in the UIs)

1. **qBittorrent**: temporary password is in the container log
   (`kubectl -n media logs deploy/arr-qbittorrent -c qbittorrent`). Set a
   real one, then Options → Web UI → **bypass authentication for clients on
   localhost** (gluetun's port-forward push depends on it). The push only fires
   when the forwarded port changes or the tunnel comes back up, so if you enable
   the bypass after the pod is already running, its first attempt has already
   403'd — `kubectl -n media rollout restart deploy/arr-qbittorrent` to re-fire
   it, then check the listen port matches `/v1/portforward`. Downloads →
   default save path `/media/downloads`; enable categories `movies` → 
   `/media/downloads/movies`, `tv` → `/media/downloads/tv`, and set
   **Default Torrent Management Mode: Automatic** or the category save paths
   are decorative (see below).
2. **SABnzbd**: the API key is in Config → General (nothing is preseeded).
   Config → Servers → add the provider; Config → Folders → temporary
   `/media/downloads/incomplete`, completed `/media/downloads`; Config →
   Categories → `tv` → `/media/downloads/tv`, `movies` →
   `/media/downloads/movies`, matching the qBittorrent categories so both
   clients land in the same place and imports stay hardlinks either way.
   Config → General → **Host whitelist** must include
   `arr-sabnzbd.media.svc.cluster.local` and the ingress host, or every
   request through the Service arrives as an unknown Host and 403s.
3. **Prowlarr**: add your indexers. Settings → Apps → add Sonarr and Radarr
   (their URLs + API keys from Settings → General); Prowlarr then syncs
   indexers into both.
   - Cloudflare-gated indexers (1337x, EZTV, KickAss, ExtraTorrent…) will
     not even *save* without FlareSolverr — Prowlarr test-fetches on add and
     the challenge fails it. Settings → Indexer Proxies → FlareSolverr at
     `http://arr-flaresolverr.media.svc.cluster.local:8191/`, give it a tag,
     put the same tag on each blocked indexer.
   - Usenet indexers are added the same way; they carry an API key instead
     of a URL, and sync to Sonarr/Radarr as `usenet` protocol.
4. **Sonarr**: root folder `/media/tv`; Download Client → qBittorrent at
   `arr-qbittorrent.media.svc.cluster.local:8080`, category `tv`, **and**
   SABnzbd at `arr-sabnzbd.media.svc.cluster.local:8080`, category `tv`.
   Both stay enabled — protocol decides which one a release goes to.
5. **Radarr**: root folder `/media/movies`; same two clients, category
   `movies`.
6. **Jellyseerr**: see [`media/jellyseerr`](../jellyseerr/README.md) — it
   talks to Sonarr/Radarr, not to this pod.

Indexers, download clients and quality profiles all live in the apps' config
PVCs, not in this chart — there is no env-var surface for any of them. The
PVCs are in k8up's backup, which is the only reason that is survivable.

Sonarr/Radarr reach qBittorrent *through* gluetun's firewall:
`FIREWALL_INPUT_PORTS=8080` admits the web UI, `FIREWALL_OUTBOUND_SUBNETS`
covers the k3s pod/service ranges for the return path.

## qBittorrent queueing and seeding

These live in the config PVC, not in this chart — qBittorrent has no env-var
surface for them, so they are set once in Options and written down here.
Options → BitTorrent → Torrent Queueing:

| Setting | Value | Why |
|---|---|---|
| Maximum active downloads | 5 | 3 was the default and it queues a season pack behind two movies. 10 works too, it just splits the same line 10 ways and every item finishes late instead of some finishing early. |
| Maximum active uploads | 10 | Seeders shouldn't starve each other once the count grows. |
| Maximum active torrents | 30 | **This is the one that bites.** Seeding torrents count against it, and nothing here ever stops seeding, so a total of 5 means that after five completed grabs the sixth download never starts — it sits queued forever while five permanent seeders hold every slot. Raise it well past the download limit, or set −1. |
| Do not count slow torrents | on | Idle seeders stop consuming active slots at all, which is the real fix for the row above. |

**Seeding stops at ratio 1.0** — Options → BitTorrent → Share Limits, "When
ratio reaches 1.0" with action **Pause torrent**. Give back what you took,
then stop.

Pause, not "Remove torrent and files": the removal action would unlink the
download-side copy while Sonarr still believes it owns that queue item.
Pausing is also fully reversible — raise the ratio and they resume where
they left off. It reclaims no disk, which is fine, because seeding costs
almost none here in the first place: Sonarr/Radarr import by hardlink, so
`/media/downloads/…` and `/media/tv/…` are the same inode and the seeding
copy is not a second copy. What seeding actually consumes is upload
bandwidth and the active-torrent slots above.

Ratio 1.0 is a public-tracker default. Anything private with a ratio floor
or a minimum seed time needs its own rule (per-torrent share limits, or a
category) before this global cap starves it.

## "This directory does not appear to exist" in Sonarr

> Remote download client qBittorrent places downloads in /media/downloads/tv
> but this directory does not appear to exist. Likely missing or incorrect
> remote path mapping.

Not a path mapping problem — the paths are genuinely identical on both sides
(one NFS export at `/media` everywhere). The cause is that qBittorrent's
**Default Torrent Management Mode is Manual**, so a torrent added by Sonarr
uses the *global* save path `/media/downloads` and ignores the `tv`
category's `/media/downloads/tv` entirely. The category reports that path to
Sonarr over the API, Sonarr looks for it, and it has never been created.

Downloads keep working throughout — Sonarr tracks the torrent's actual
`content_path` — which is why this reads as a false alarm.

Fixed on 2026-08-11 by setting Options → Downloads → **Default Torrent
Management Mode: Automatic**, so new torrents land in their category folder.
Existing torrents stay Manual and are not moved; the 259 already in flight
finish where they are. `/media/downloads/{tv,movies}` were also created by
hand (`abc:users`, matching their parent) — Automatic mode alone would not
have made them until the next grab, and the health check wants them there
now. Sonarr caches health results, so force a re-run rather than waiting:

```bash
kubectl -n media exec deploy/arr-sonarr -- sh -c \
  'k=$(grep -o "<ApiKey>[^<]*" /config/config.xml | head -1 | cut -c9-); \
   curl -s -X POST -H "X-Api-Key: $k" -H "Content-Type: application/json" \
     -d "{\"name\":\"CheckHealth\"}" http://localhost:8989/api/v3/command'
```

## If gluetun crash-loops with a tun EPERM

`gluetun.privileged: true` — same device-cgroup story as Jellyfin's
`/dev/dri`: unprivileged containers are denied host device nodes and NET_ADMIN
alone isn't always enough on containerd.
