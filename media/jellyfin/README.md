# jellyfin — the media server

Jellyfin serving `/mnt/vault/media` (NFS, read-only) with VAAPI hardware
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

`/media/movies` and `/media/tv` on the NAS's existing `media` dataset,
mounted read-only. The NFS export was created for this namespace
(maproot=root, LAN-only, same shape as democratic-csi's exports); the SMB
`media` share is the same directory, so anything dropped there from a desktop
appears in the library too. First-run wizard: add `/media/movies` as a
Movies library and `/media/tv` as Shows.

Metadata, artwork and watch state live in the 30Gi `truenas-iscsi` config
PVC (SQLite — same reasoning as RomM's DB), which k8up backs up by default.
The library itself is not a PVC and is never backed up from here.
