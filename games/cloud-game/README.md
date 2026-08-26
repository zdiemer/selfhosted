# cloud-game (CloudRetro) — retro multiplayer in the browser

Runs [cloud-game](https://github.com/giongto35/cloud-game) on the cluster: a
libretro emulator (NES, SNES, GB/GBA, N64, PS1, FBNeo arcade, DOSBox) executes
server-side, the video is encoded to VP8 and streamed to the browser over
WebRTC, and controller input comes back the same way. Several people opening
the same room link each get a controller port — four-player Mario Kart 64
with nothing installed on the client.

ROMs come read-only from the same NAS share RomM uses.

## Architecture

```
browser ──HTTPS/WSS──▶ Traefik (+Authelia) ──▶ coordinator :8000 ─┐
browser ◀══UDP 8443 (WebRTC media+input)══▶ worker ◀──localhost──┘
                                              │  └─ Xvfb :99 (shared X socket)
                                              ├─ SMB (RO): Roms/<platform> → assets/games/<core>
                                              └─ PVC: saves/ + assets/cores/
```

One pod, three containers (xvfb, coordinator, worker), `Recreate` strategy.

## The UDP caveat

Media never touches Traefik. After the websocket handshake the browser sends
UDP straight to `webrtc.iceIpMap:8443`, which klipper-lb publishes on every
node IP (and on every node's tailnet address, since the hostPort is bound on
all interfaces). That means:

- It does NOT work through the Cloudflare tunnel (HTTP only), so there is
  deliberately no `cloudflareHosts`. Internet friends would need a port
  forward or a TURN server — follow-up, not v1.
- **`iceIpMap` must be a tailnet address.** A LAN address there looks correct
  and fails with no error anywhere — see below. It is currently a node's
  100.x, which serves LAN and remote players alike.

### Why a LAN `iceIpMap` deadlocks (cost a long debug session, 2026-08-26)

The symptom is the splash screen never becoming the game list: the menu is
only drawn from `onConnect`, so a stalled WebRTC connection looks like an
empty library. The worker just logs `ice: failed` after 30s.

Two things have to line up, and with a LAN `iceIpMap` neither does:

1. **The worker cannot ping first.** Browsers hide their LAN IP behind an mDNS
   `<uuid>.local` host candidate (RFC 8828), and upstream hardcodes
   `SetICEMulticastDNSMode(MulticastDNSModeDisabled)`, so pion discards all of
   them: `Remote mDNS candidate added, but mDNS is disabled`, then `Failed to
   ping without candidate pairs` every 200ms until it gives up.
2. **So the browser has to ping first** — normally fine, since that teaches the
   worker a peer-reflexive candidate. But `retro.*` resolves to a tailnet IP,
   so the browser gathers *tailnet* candidates and binds its sockets there, and
   nothing advertises a subnet route for `192.168.4.0/22`. A LAN `iceIpMap` is
   simply unreachable from that socket.

Both sides stay silent and nothing reaches the wire — which makes it look
exactly like a firewall or a routing problem. It is neither: probe
`iceIpMap:8443` with `nc -u` from the player's machine and the packets arrive
fine. Set `webrtc.logLevel: 0` and read the worker log instead; it names the
discarded candidates outright.

Confirming from the browser: `about:config` →
`media.peerconnection.ice.obfuscate_host_addresses: false` makes Firefox
advertise its real IP, and the connection succeeds instantly. That is a
diagnostic, not a fix — a tailnet `iceIpMap` is what makes it work for
everyone with stock browser settings.

## Deploy

1. Create the vault item `games-cloud-game` with a `values.local.yaml` field
   (see `values.local.yaml.example`; the RomM NAS user is fine).
2. `./build.sh` — compiles the upstream Dockerfile at the pinned master commit
   (`image.upstreamRef`) and pushes `registry.zachd.duckdns.org/zdiemer/cloud-game:<tag>`. First build is slow (GStreamer from
   source). Make the package public after the first push.
3. `./upgrade.sh`.
4. Open https://retro.zachd.duckdns.org, sign in to Authelia, pick a game.
   Share the room URL for multiplayer.

The worker fetches the libretro cores from the buildbot on first start into
the data PVC, so the first game load takes a minute.

## Library

cloud-game wants one folder per core; the NAS is per platform, and most sets
are one zip per game, which cloud-game can't open (only the mame core takes
zips). So `library.platforms` in values.yaml has three modes:

| mode | what happens | for |
|---|---|---|
| `unzip: true` + `include` | init container extracts the matches onto the data PVC | zipped sets (NES, SNES, N64, GBA) |
| `unzip: false` + `include` | init container copies the matching files onto the data PVC | bare-file sets you want curated (PSX .chd) |
| `unzip: false`, no `include` | the NAS folder is subPath-mounted directly | small folders where you want everything |

The first two are idempotent — a game already staged costs a stat, not a
re-copy — and games dropped from `include` are pruned. Edit the list,
`./upgrade.sh`, the pod restarts and the dropdown follows.

Curate deliberately: cloud-game puts every ROM it finds in ONE dropdown, and
it only renders after the WebRTC connection is up. The PSX set is 5,529 discs;
mounted whole it made the coordinator's init message 387 KB. Note that
`unzip: false` + `include` COPIES the ROMs, so watch `persistence.data.size`
(the shipped PSX list is ~4 GB).

Add a core by adding an entry — the key must be a core name cloud-game knows
(`nes`, `snes`, `gba`, `n64`, `psx`, `mame`, `dos`).

## Upgrading

Pick a new upstream master commit, set `image.upstreamRef` to its full sha and
`image.tag`/`appVersion` to `main-<short sha>`, `./build.sh`, `./upgrade.sh`.
Renovate can't see this one: the image doesn't exist until we build it, and
upstream hasn't tagged a release since 2023.
