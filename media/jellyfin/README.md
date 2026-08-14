# jellyfin — the media server

Jellyfin serving `/mnt/vault/media` (NFS, read-write) with VAAPI hardware
transcoding on `zachd-ubuntu-1` — the Ryzen 3 2200G, the only node with a
real GPU. Movies and TV land in the library via the sibling
[`media/arr`](../arr/) stack; requests come in through
[`media/jellyseerr`](../jellyseerr/).

## Access

| From | Address | Notes |
|---|---|---|
| Anywhere | `https://jellyfin.zachd.duckdns.org` | Traefik ingress, wildcard cert |
| LAN (smart TVs) | `http://<any-node-ip>:30096` | NodePort, no DNS/TLS involved |

Native apps exist for basically every TV platform — Samsung Tizen, LG webOS,
Android TV / Google TV, Fire TV, Roku, Apple TV — plus iOS/Android and the
web UI. Point the app at either address. **Quick Connect** (enable in
Dashboard → General) is the TV sign-in you actually want: the TV shows a
6-digit code, you approve it from your phone's browser, no typing passwords
with a remote.

**Deliberately not behind Authelia.** The TV/phone apps speak the Jellyfin
API directly and cannot complete a forward-auth portal bounce. Jellyfin's own
account system is the gate — same posture as RomM's built-in login.

## Transcoding

The pod is pinned to `zachd-ubuntu-1` and mounts `/dev/dri` (privileged: the
device cgroup denies host device nodes to unprivileged containers, and the
cluster runs no GPU device plugin). `transcode.renderGid: 992` is that node's
`render` group. After install, finish the job in the UI:

Dashboard → Playback → Transcoding → **VAAPI**, device `/dev/dri/renderD128`.
Enable hardware decode/encode for H.264 and HEVC (Vega does both directions).
Leave AV1 off — VCN 1.0 can't.

If the GPU node is down, `transcode.enabled: false` unpins the pod and drops
the privileged bit; playback falls back to direct play + CPU transcode.

## Library

`/media/movies` and `/media/tv` on the NAS's existing `media` dataset. The
NFS export was created for this namespace (maproot=root, LAN-only, same shape
as democratic-csi's exports); the SMB `media` share is the same directory, so
anything dropped there from a desktop appears in the library too. First-run
wizard: add `/media/movies` as a Movies library and `/media/tv` as Shows.

### How new media actually shows up

Not through real-time monitoring, even though it's switched on for both
libraries. That's inotify against an NFS mount, and inotify doesn't see
writes from other NFS clients — which is every import, since Sonarr and
Radarr write to `/media` from their own pods.

What works instead:

1. **Sonarr/Radarr → Connect → Emby / Jellyfin** (`Update Library` on),
   pointed at `jellyfin.media.svc.cluster.local:8096` with a Dashboard → API
   Keys key. On import it POSTs the changed folder to
   `/Library/Media/Updated` and that path is rescanned inside a minute
   (`LibraryMonitorDelay`, 60s). This is the path that matters — see
   [`media/arr`](../arr/README.md#the-jellyfin-rescan-is-not-optional).
2. **Scheduled "Scan Media Library"**, set to every 1h (Jellyfin's default is
   12h). Backstop for files dropped straight onto the SMB share, which
   nothing announces. A full scan measures ~10s here, so the interval is
   cheap.

Both live in the config PVC, not in this chart — the API key is a runtime
secret and the task trigger is Jellyfin state, so neither is templated.
Rebuilding the config PVC from scratch means redoing both, and the symptom
if you forget is exactly the one that led here: the library only updates
when you scan by hand.

Metadata, artwork and watch state live in the 30Gi `truenas-iscsi` config
PVC (SQLite — same reasoning as RomM's DB), which k8up backs up by default.
The library itself is not a PVC and is never backed up from here.

## Deleting media from the UI

The mount is **read-write** (`media.readOnly: false`), so Dashboard → a
title → Delete removes the files for real. Two things gate it, and both are
easy to trip over:

1. **The mount.** Read-only until Aug 2026; if delete fails with a
   permissions error after a chart change, check that *both* the volume and
   the volumeMount dropped `readOnly` — the volume's flag wins on its own.
2. **The user policy.** Jellyfin's "Allow media deletion" is off for every
   user by default (Dashboard → Users → *user* → Allow media deletion, and
   per-library beneath it). Since Jellyfin is deliberately not behind
   Authelia, this checkbox is the only thing standing between a household
   account and the library — grant it to the admin account only.

Deletion is immediate and permanent: no trash, no restore, and `/media` is
not backed up (re-acquirable by definition). It also does **not** tell
Sonarr/Radarr anything — the series/movie stays monitored there and gets
re-downloaded on the next search. To actually be rid of something, delete it
in Sonarr/Radarr instead (or afterwards), which removes the file *and* stops
the automation chasing it.
