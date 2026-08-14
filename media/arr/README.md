# arr — acquisition for the media library

Prowlarr + Sonarr + Radarr + qBittorrent-inside-gluetun, the automation
behind [Jellyfin](../jellyfin/). The flow:

```
Jellyseerr request → Radarr/Sonarr → search via Prowlarr's indexers
  → grab handed to qBittorrent (all traffic inside the PIA tunnel)
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
2. **Prowlarr**: add your indexers. Settings → Apps → add Sonarr and Radarr
   (their URLs + API keys from Settings → General); Prowlarr then syncs
   indexers into both.
3. **Sonarr**: root folder `/media/tv`; Download Client → qBittorrent at
   `arr-qbittorrent.media.svc.cluster.local:8080`, category `tv`.
4. **Radarr**: root folder `/media/movies`; same download client, category
   `movies`.
5. **Jellyseerr**: see [`media/jellyseerr`](../jellyseerr/README.md) — it
   talks to Sonarr/Radarr, not to this pod.
6. **Jellyfin rescan** (both Sonarr and Radarr): Settings → Connect → add
   **Emby / Jellyfin**, host `jellyfin.media.svc.cluster.local`, port 8096,
   API key from Jellyfin's Dashboard → API Keys, **Update Library on**.
   See below — without this, new media takes up to an hour to appear.

### The Jellyfin rescan is not optional

Jellyfin has real-time monitoring enabled on both libraries, and it does
nothing for us: it is inotify on an NFS mount, and inotify never sees writes
made by another NFS client. Every import in this stack is exactly that —
Sonarr and Radarr write to `/media` from their own pods, on their own nodes.
So Jellyfin learns about new files from two places only, and this Connect is
the fast one: on import/upgrade/rename/delete it POSTs the changed folder to
Jellyfin's `/Library/Media/Updated`, which rescans just that path within
about a minute (`LibraryMonitorDelay`, 60s).

The other place is Jellyfin's scheduled "Scan Media Library" task, now at 1h
(default is 12h). That is the backstop for files nobody announced — anything
dropped into the SMB share from a desktop — not the path for arr imports. A
full scan of this library measures ~10s, so the hourly cost is noise.

Leave **Send Notifications** off: that's an Emby feature, Jellyfin returns an
error for it, and it's what makes Connect's *Test* button fail even though
the library update itself works fine. Save past the test.

Paths need no mapping: one NFS export at `/media`, identical on both sides.

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
