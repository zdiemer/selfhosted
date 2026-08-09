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
   localhost** (gluetun's port-forward push depends on it). Downloads →
   default save path `/media/downloads`; enable categories `movies` → 
   `/media/downloads/movies`, `tv` → `/media/downloads/tv`.
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

## If gluetun crash-loops with a tun EPERM

`gluetun.privileged: true` — same device-cgroup story as Jellyfin's
`/dev/dri`: unprivileged containers are denied host device nodes and NET_ADMIN
alone isn't always enough on containerd.
