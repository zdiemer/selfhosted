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
browser ──HTTPS/WSS──▶ Traefik (+auth) ──▶ coordinator :8000 ─┐
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

- The tunnel carries the coordinator's HTTP + `/ws` and nothing else. Media
  cannot cross it and no setting changes that: cloudflared proxies HTTP
  origins, and Cloudflare's UDP/TCP support needs WARP on the *viewer's*
  machine, which a browser tab cannot do. Internet players therefore relay
  through TURN — see below.
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

### Internet players: TURN, not the tunnel

`webrtc.iceServers` points at coturn on the egress VPS
(`infra/egress-proxy/vps`, `TURN_RELAY=true`), and `retro.diemer.codes` is in
`ingress.cloudflareHosts`. The worker then offers three candidates:

| candidate | priority | who ends up on it |
|---|---|---|
| `<tailnet-ip>:8443 typ host` | 2130706431 | LAN and tailnet players — direct |
| `<home-public-ip>:<port> typ srflx` | 1694498815 | only if the home NAT cooperates |
| `<vps-ip>:<port> typ relay` | 1023 | everyone else |

**A relay candidate is additive, which is the whole reason this works.** ICE
ranks it last, so local players keep the direct path and only internet players
pay the SFO3 round trip. Pointing `iceIpMap` at the VPS instead would have been
simpler and wrong: it is single-valued, so it would have dragged LAN and
tailnet players out to SFO3 too. It also means the worker needs no inbound port
anywhere.

Bandwidth is the running cost. vp8 is pinned at 3.2 Mbit/s and every viewer
gets their own peer connection, so a 4-player room is ~12.8 Mbit/s out of the
house and ~5.8 GB per hour through the VPS in each direction.

The TURN credential is a static long-term one, so **it is handed to the browser
in the coordinator's INIT message** — any player can read it. That is inherent
to TURN without a credential-minting API, and it is why coturn denies RFC1918
*and* 100.64/10 as peers (an allocation can never be aimed into the tailnet)
and caps `user-quota`/`total-quota`/`max-bps`. The page is still gated (see
below), so it is authenticated players only.

### Two hosts, two gates

cloud-game has no login of its own — anyone who reaches the coordinator can
pick a ROM and play — so both hosts need a gate, but not the same one.

| host | gate | why |
|---|---|---|
| `retro.zachd.duckdns.org` (tailnet) | Authelia forward-auth | it is you, and you already have an account |
| `retro.diemer.codes` (public) | one shared password | it is friends, and a room is shared anyway |

Per-guest Authelia accounts would buy nothing here: everyone who opens the room
link drives a controller port on the *same* emulator instance, so there is no
per-user state to protect — only the front door. The password lives in
`values.local.yaml` (`auth.basicAuth.password`); rotate it with
`scripts/secrets.sh edit games/cloud-game` and re-run `./upgrade.sh`.

The middleware annotation is per-Ingress, so this is why there are **two**
Ingress objects — one host each. Putting both on one object would apply the
same gate to both. `infra/ingress-policy` rule 4 was taught to accept a
`basicauth` middleware alongside `forwardauth`: the question it asks is "did
anyone decide", and the alternative was to label a password-protected host
`public-unauthenticated: true`, which would make the audit trail a lie.

Basic auth survives the `/ws` upgrade — the browser replays the cached
credentials on the WebSocket handshake (verified: `101` with them, `401`
without). Note that a raw `curl` test of this needs `--http1.1`; over HTTP/2
`Connection: Upgrade` is invalid and Traefik answers `400`, which looks like an
auth failure and is not one.

Manual step, one time: add `retro.diemer.codes` as a Public Hostname on the
tunnel in the Cloudflare dashboard (→ `https://traefik.kube-system.svc.cluster.local:443`,
**No TLS Verify on**). Nothing in git can create that.

## Deploy

1. Create the vault item `games-cloud-game` with a `values.local.yaml` field
   (see `values.local.yaml.example`; the RomM NAS user is fine).
2. `./build.sh` — compiles the upstream Dockerfile at the pinned master commit
   (`image.upstreamRef`) and pushes `registry.zachd.duckdns.org/zdiemer/cloud-game:<tag>`. First build is slow (GStreamer from
   source). Make the package public after the first push.
3. `./upgrade.sh`.
4. Open https://retro.zachd.duckdns.org, sign in to Authelia, pick a game.
   Share the room URL for multiplayer. Friends off the tailnet use
   https://retro.diemer.codes and the shared password instead.

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

A pattern matching nothing is only a `stage: <core>: WARNING no match for
include: ...` line in the init container log — the pod still starts, one game
short. Check it after editing the lists.

The lists are curated for couch multiplayer, which is bounded by the console,
not the chart: cloud-game runs one emulator instance and gives each person who
opens the room link a controller port. n64 has 4, nes/snes/psx have 2 (a few
titles reach 4 via an emulated multitap), and **gba has 1** — there is no
link-cable emulation, so GBA stays single-player however many people join.
Multi-disc PSX games are left out on purpose: there is no disc-swap UI, so
anything spanning discs can't be finished.

Add a core by adding an entry — the key must be a core name cloud-game knows
(`nes`, `snes`, `gba`, `n64`, `psx`, `mame`, `dos`).

## Upgrading

Pick a new upstream master commit, set `image.upstreamRef` to its full sha and
`image.tag`/`appVersion` to `main-<short sha>`, `./build.sh`, `./upgrade.sh`.
Renovate can't see this one: the image doesn't exist until we build it, and
upstream hasn't tagged a release since 2023.
