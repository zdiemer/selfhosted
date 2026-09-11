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

## Playback: what actually happens to a stream

Worth knowing before chasing a playback bug, because the answer is rarely
"the GPU". Most of this library is x265 in MKV — a representative episode is
HEVC Main 10, EAC3 5.1, SRT subtitles. In a browser that fully determines the
path:

- **No browser plays MKV**, so every web session is repackaged to HLS fMP4 on
  the fly. This is unavoidable and has nothing to do with quality settings.
- HEVC video is **copied**, not re-encoded (`-codec:v:0 copy`).
- EAC3 5.1 *is* re-encoded, to stereo AAC, because no browser takes EAC3.
  (`-af volume=2` in the ffmpeg line is `DownMixAudioBoost`, not a bug.)

So the common case is a **DirectStream**: a remux plus an audio encode. The
GPU is never touched. `transcode.enabled` buys nothing for this content — it
earns its keep only on files that need a real video encode (burned-in
subtitles, bitrate-capped remote clients, codecs the client can't decode).

### Pause/resume stutter, and video freezing while audio keeps playing

One root cause, two symptoms. **Jellyfin does not keep one ffmpeg alive
across a seek.** It kills the process and starts a new one at the new offset,
and each restart re-probes the source with `-analyzeduration 200M -probesize
1G` — over NFS — before emitting a fresh fMP4 init segment. That is seconds
of dead air per restart, and the restarts come in bursts; six in eight
seconds is an ordinary-looking stretch of `/config/log/log_*.log`:

```
18:22:48  ffmpeg ... -start_number 0
18:22:50  Stopping ffmpeg process with q command
18:22:51  ffmpeg ... -ss 00:52:06.292 -start_number 303
18:22:52  ffmpeg ... -ss 00:51:14.157 -start_number 298
18:22:54  ffmpeg ... -ss 00:50:42.876 -start_number 295
```

On desktop that reads as stuttering. On the official iOS app it reads as
audio resuming without video: iPhone Safari has no MSE, so the app (a
WKWebView wrapper around the same web client) hands the `.m3u8` to native
AVPlayer, and AVPlayer handles a mid-stream discontinuity plus a new init
segment far less gracefully than hls.js does — the audio pipeline recovers,
the video decoder stays wedged.

Nothing server-side fixes this properly, because the restart is the design.
What fixes it is **a client that direct-plays the file** and never involves
ffmpeg at all:

| Client | Result |
|---|---|
| [Jellyfin Media Player](https://github.com/jellyfin/jellyfin-media-player) (desktop, mpv) | Direct play — MKV/HEVC/EAC3 as-is, instant seek |
| [Swiftfin](https://github.com/jellyfin/Swiftfin) (iOS) | Direct play **only on the VLCKit player** — see below |
| Browser / official iOS app | Always remuxed; restarts on every seek |

### Subtitles vanishing mid-playback, and missing in picture-in-picture

Also downstream of the remux. Note `-map -0:s` in the ffmpeg line: subtitles
are deliberately **excluded** from the stream and delivered as a separate
fetch, which the web client paints as an HTML overlay on top of the `<video>`
element.

Two consequences follow directly:

1. **They disappear on an ffmpeg restart** and don't come back — the player
   re-initialises and the subtitle track is not always re-attached. Made
   worse by lazy extraction: the first request for an embedded SRT shells out
   to ffmpeg to demux it to `/config/data/subtitles`, which measured **11
   seconds** over NFS here. Only 15 files are cached, so nearly every episode
   pays it.
2. **They can never appear in picture-in-picture.** iOS PiP promotes the
   video layer alone; an HTML overlay is not part of it. This is structural,
   not a setting.

Levers, cheapest first:

- **Extract sidecar `.srt` files** with [`extract-subtitles.sh`](./extract-subtitles.sh).
  There is no server-side setting for this — 10.11 has no "extract subtitles"
  scheduled task, and the per-library extraction options cover chapter images
  and trickplay only (`/config/root/default/*/options.xml`). Sidecars are read
  straight off disk with no ffmpeg in the path, so the 11-second stall and the
  race behind it are gone for every client at once, and unlike
  `/config/data/subtitles` they survive a config PVC rebuild. Audit mode is the
  default; see below for what a real run plans. Does nothing for PiP.
- **Always burn in subtitles when transcoding** (Dashboard → Playback). Makes
  subtitles pixels, so they cannot desync, vanish, or be dropped by PiP — the
  only route to subtitles *in* PiP, and the only option at all for the bitmap
  (PGS/VOBSUB) tracks that cannot become sidecars. The cost is real: video can
  no longer be copied, so every subtitled stream becomes a full VAAPI encode on
  `zachd-ubuntu-1` — the node already at ~87% CPU requested. The toggle does
  exist in this build (it is in the 10.11 web bundle), but there is an [open
  report that burn-in regressed in
  10.11](https://github.com/jellyfin/jellyfin-web/issues/7254), so confirm it
  fires before relying on it.
- **Swiftfin on the VLCKit player.** Renders subtitles inside the player, so
  both problems go away and so does the freeze — but it has no PiP at all.
  See below: on iOS this is a genuine either/or, not a straight upgrade.

#### What a sidecar run actually plans

Unfiltered, the library plans **~4,300 sidecars** — REMUX releases here carry
15 to 45 subtitle languages each, and one `.srt` per language is clutter in the
folder and noise in every track picker. So the script defaults to English only,
first track per language. On a 12-file sample that is 9 sidecars instead of 95.

**Budget the run by bytes, not by file count.** ffmpeg has to demux the whole
file to reach the subtitle packets — they are interleaved throughout a
Matroska, not seekable to — so cost tracks file *size*. Measured here: a 1.3GB
episode takes ~32s, a 22GB REMUX takes minutes. Across the 1.8TB library that
is roughly **12 hours of continuous NFS reads**, competing with playback on the
same mount and node the entire time.

So scope it rather than running it whole: `--path /media/tv/<show>` for a series
you are about to watch (FROM's 40 episodes are ~21 minutes), and `--max-gb` to
skip the movie remuxes — a 22-minute read to save an 11-second stall on a film
watched once is a bad trade, while a series you watch eight episodes of back to
back is a good one. Run it when nobody is watching.

Two things the audit surfaces that no setting can fix:

- **Bitmap subtitles.** 62 PGS/VOBSUB streams in those same 12 files. They are
  images, not text — they cannot become `.srt` at any quality. A file whose
  only English subtitle is PGS needs burn-in or nothing.
- **Duplicate unflagged tracks.** Several releases carry two English SRTs with
  neither `forced` nor `hearing_impaired` set — the *From* episodes are the
  case examined above, where track 3 is plainly the SDH variant (849 cues vs
  739) and the file says nothing about it. The script takes the first and
  reports the rest rather than guessing; `--all-tracks` includes them, suffixed
  by stream index.

### Swiftfin: subtitles wildly out of sync

Swiftfin offers two backends — **Swiftfin (VLCKit)** and **Native (AVPlayer)**
— and the choice decides everything here. AVPlayer cannot open MKV, so on the
native player a file that reports `SupportsDirectPlay: true` still gets
remuxed. From a real session (`Swiftfin iOS 1.6`, S01E01):

```
-map 0:0 -map 0:1 -map -0:s -codec:v:0 copy -codec:a:0 copy
-copyts -start_at_zero -f hls
```

Both streams copied — so the *only* reason for the remux is the container —
subtitles stripped (`-map -0:s`) and delivered externally, against a stream
muxed with `-copyts`. That combination is the well-known [subtitle desync on
transcoded/remuxed streams](https://github.com/jellyfin/jellyfin/issues/11825):
rock solid on direct play, drifting the moment the stream goes through the
HLS pipeline, and worse after every seek.

The obvious fix is **Settings → Playback → Swiftfin (VLCKit)**, which the
maintainers recommend anyway: VLCKit opens the MKV directly and reads the
*embedded* subtitle track, so no remux, no sidecar, no `-copyts`, no drift.

But it is not a free upgrade, because the two backends split the features
this library needs down the middle
([players.md](https://github.com/jellyfin/Swiftfin/blob/main/Documentation/players.md)):

| | Picture-in-picture | MKV direct play | SRT subtitles |
|---|---|---|---|
| Swiftfin (VLCKit) | ❌ | ✅ | ✅ |
| Native (AVPlayer) | ✅ | ❌ — always remuxed | ❌ |

The native player supports only CC_DEC, TTML and VTT, and its subtitle *track
selection* is documented as not working at all over HLS. So the desync was
never really a sync bug — SRT is simply not a supported path on that
backend.

That leaves two coherent setups, and no third:

- **VLCKit** — direct play, exact sync, subtitles work, zero server load.
  No picture-in-picture.
- **Native + burned-in subtitles** — PiP works *and* shows subtitles, because
  they are pixels in the video rather than a track AVPlayer has to render.
  Sync becomes structurally impossible to get wrong. The native player is
  already forcing a remux of every file here, so the marginal cost is turning
  that copy into a real VAAPI encode.

If you take the second: because track selection is broken on that backend,
select nothing in the client — set the subtitle language and mode ("Always")
in Dashboard → Users → *user*, so the **server** picks the track and burns it
without the client ever asking. And confirm burn-in actually fires first;
there is an [open report that it regressed in
10.11](https://github.com/jellyfin/jellyfin-web/issues/7254).

This fork in the road is temporary: Swiftfin is [collapsing the two players
into one](https://github.com/jellyfin/Swiftfin/issues/1853), an
`AVMediaPlayerProxy` driving an `AVPlayerLayer`, which should give VLCKit's
subtitle handling and AVPlayer's PiP together. Worth re-testing after that
lands rather than living with burn-in forever.

Worth ruling out while you're here, because these files invite the theory:
the episodes carry **two English SRT tracks** (indices 2 and 3) with
identical titles. They are not different timings — track 3 is simply the SDH
variant (849 cues vs 739, speaker labels added); both open at `00:00:00,613`
and close at `00:47:03,607`. Picking the "wrong" one costs you `[Narrator]`
tags, not sync.

### Transcode cache growth

`EnableSegmentDeletion` is **off**, which is why `/cache` grows the way
`persistence.cacheSizeLimit` in `values.yaml` describes: with no throttling
and no deletion, ffmpeg races ahead and every finished segment is kept until
the session is reaped. Turning segment deletion on (Dashboard → Playback,
with `SegmentKeepSeconds` at its 720s default) bounds it to a rolling window
and is the lever the values comment is pointing at. The trade-off is that
seeking backwards past that window forces one of the restarts above.

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

## Skip intro / recap / credits

Jellyfin has had a native **Media Segments** API since 10.10 — timed regions
of type `Intro`, `Outro`, `Recap`, `Preview` or `Commercial` that clients turn
into a skip button. The server ships no *provider* for them, so the segments
have to come from a plugin.

[**Intro Skipper**](https://github.com/intro-skipper/intro-skipper) is the
one to use. It chromaprint-fingerprints the audio of every episode in a
series, finds the span they share, and writes it out as a segment — so it
covers recaps and end credits, not just the title sequence. Since 10.10 it no
longer patches the web UI, which is what used to make it break on every
upgrade.

Install (all runtime state, nothing to template):

1. Dashboard → Plugins → Repositories → add `https://intro-skipper.org/manifest.json`.
   That endpoint serves a manifest matched to the requesting server's version.
2. Catalog → Intro Skipper → install, then restart the pod.
3. Dashboard → Scheduled Tasks → **Detect and Analyze Media Segments**.
4. Per client, in its playback settings: *ask to skip* vs *auto skip*.

Three things to know before running it:

- **The plugin build is pinned to the server version** — the current release
  wants 10.11.11 or newer, and a mismatched build fails to load or crashes on
  startup. Renovate bumps `image.tag` here unattended, so a Jellyfin bump can
  silently strand the plugin; `renovate.json` labels those PRs
  `needs-plugin-check` for that reason. The ffmpeg requirement
  (jellyfin-ffmpeg 7.1.1-7+) is already satisfied by the official image.
- **The first analysis pass is a genuine CPU load**, on the node that is
  already ~87% CPU-requested and hosting talaria — and this pod has no CPU
  limit, deliberately (see `values.yaml`). Kick it off in an idle window;
  incremental runs afterwards are cheap.
- **Client support is uneven at the edges.** Web, Android, Android TV and iOS
  are fine. webOS auto-skips but [won't render the *ask to skip*
  button](https://github.com/jellyfin/jellyfin-webos/issues/272). Roku and
  Kodi don't auto-skip.

Everything lives in the config PVC, so it survives image bumps but not a PVC
rebuild — same caveat as the arr webhook and the scan interval above.

If the analysis cost is the objection,
[TheIntroDB](https://github.com/TheIntroDB/jellyfin-plugin) is a crowdsourced
provider that fetches known timestamps instead of computing them locally:
near-zero CPU, thinner coverage on obscure titles. The two compose — run both
and let Intro Skipper fill the gaps.

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
