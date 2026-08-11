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

**Seeding is unlimited, deliberately.** No ratio limit, no seeding-time
limit, no share-limit action — torrents seed until removed by hand. That is
close to free here: Sonarr/Radarr import by hardlink, so the file in
`/media/downloads` and the one in `/media/tv` are the same inode and the
seeding copy costs no extra bytes. Deleting the torrent *with data* just
drops one of the two links; the library keeps the file. The cost is upload
bandwidth and the active-torrent slots above, not disk.

If disk ever does become the pressure, the lever is Options → BitTorrent →
Share Limits (ratio 2.0 / 30 days, action "Remove torrent" — *not* "Remove
torrent and files", which would unlink the download-side copy while Sonarr
still believes it owns it).

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
`content_path` — which is why this reads as a false alarm. Fix it properly by
setting Options → Downloads → **Default Torrent Management Mode: Automatic**;
new torrents then land in the category folder and the warning clears.
Existing torrents stay Manual (nothing gets moved) unless you switch them
individually. Creating the two directories by hand also silences the warning,
but leaves every file piling up in the root of `/media/downloads`.

## If gluetun crash-loops with a tun EPERM

`gluetun.privileged: true` — same device-cgroup story as Jellyfin's
`/dev/dri`: unprivileged containers are denied host device nodes and NET_ADMIN
alone isn't always enough on containerd.
