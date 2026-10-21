# steam-headless — Steam + Sunshine for Moonlight clients

Runs [Steam-Headless](https://github.com/Steam-Headless/docker-steam-headless)
on zachd-ubuntu-6 (Ryzen 7 6800H, Radeon 680M): Xorg + XFCE + Steam on the
real GPU, with [Sunshine](https://github.com/LizardByte/Sunshine) capturing it
and encoding H.264/HEVC on VAAPI (the 680M has no AV1 encoder). Clients are
Moonlight — "Moonlight Game Streaming" on iOS/Apple TV, plus Android, desktop
and Steam Deck builds.

First deploy `../../infra/steam-input-runtime/upgrade.sh`, then `./upgrade.sh` (secrets from `op://homelab/games-steam-headless`).

## Addresses (tailnet only)

| What | Where |
|---|---|
| Moonlight host | `zachd-ubuntu-6.taila58f7e.ts.net` (`100.86.22.101`) — works home or away; `192.168.4.22` also works on the LAN |
| Sunshine web UI (PIN entry, settings) | `https://sunshine.zachd.duckdns.org` — Authelia, then `admin` / `sunshine.password` |
| noVNC desktop (Steam login, library setup) | `https://steam.zachd.duckdns.org` — Authelia |

The two web UIs go through Traefik (`templates/ingress.yaml`). Moonlight's
stream can't: it is fixed-port UDP straight to the encoding host, which is why
the Moonlight host is ubuntu-6's own Tailscale name rather than a duckdns name
(those all resolve to whichever node infra/duckdns picked). Only streaming ports
are published through hostPort: TCP 47984/47989/48010 and UDP
47998/47999/48000/48002/48010. The pod has its own network namespace.
Desktop 8083 and Sunshine administration 47990 are reachable through ingress;
they are no longer published on the node.

## First run

1. `steam.zachd.duckdns.org` → log in to Steam (Steam Guard once). The Steam library at
   `/mnt/games` (400Gi local NVMe) is created by the image on first boot;
   check Settings → Storage and make it the default.
2. Moonlight (Tailscale on) → add host `zachd-ubuntu-6.taila58f7e.ts.net` →
   it shows a PIN → enter it on `sunshine.zachd.duckdns.org`'s PIN tab.
3. Pick "Steam Big Picture" or "Desktop" in Moonlight.

## Why the chart looks the way it does

- **Image `debian-1.0.x`, not `2.44.*`/`stable`.** Those tags on the same Docker
  Hub repo are a different image (Fedora + systemd + a "SHUI" web setup) with
  no public source yet. See values.yaml.
- **Explicit devices, no privileged mode.** Kubelet allocates card1, renderD128,
  uinput, tty0 and tty7. The main container cannot create devices, administer
  networks or mount filesystems in its original user namespace. No host directory
  or host namespace is mounted into this pod.
- **Virtual input only.** A separate container with MKNOD/CHOWN publishes virtual
  input devices into a tmpfs, read-only in Steam. Physical host input devices are
  excluded. A scoped NRI rule permits dynamic input minors. Xorg explicitly loads
  Sunshine's keyboard and mouse; SDL polls for controller hotplug. Xorg restarts
  once when Sunshine inputs first appear, and if their device numbers change.
- **Game sandboxes.** Steam-specific AppArmor/seccomp profiles allow unprivileged
  user namespaces and Bubblewrap. No SYS_ADMIN, NET_ADMIN or unconfined profile.
  Default proc masking remains; sandbox modes that insist on a fresh proc mount
  are unsupported. Upstream Flatpak's privileged proc initialization is disabled.
- **Network containment.** A dedicated policy permits public IPv4, DNS, Authelia,
  and Moonlight clients on LAN/tailnet. Client-selected UDP ports 1024–65535 are
  allowed to those networks; private TCP initiation is denied. Kubernetes policy
  does not isolate the pod from its own node. This is not VM isolation: the GPU,
  kernel, uinput and virtual consoles remain shared host attack surfaces.
- **Real Xorg driver, not the dummy config.** ubuntu-6's HDMI-A-1 reports a
  connected sink, so Xorg runs on amdgpu/glamor and OpenGL renders on the GPU.
  If that sink is removed the image falls back to the dummy DDX and OpenGL
  (Steam UI, native GL games) drops to llvmpipe; Vulkan/Proton is unaffected.
  Fix with an HDMI dummy plug.
- **local-path storage.** The pod is hostname-pinned anyway (only worthwhile
  iGPU), so the library lives on ubuntu-6's NVMe. The games PVC is
  `k8up.io/backup: "false"`; home (Steam config, Proton prefixes, Sunshine
  pairings) is backed up.

## Performance expectations

The 680M is roughly Steam Deck class or a bit better: any indie at 1080p60,
older/lighter AAA at 720p–1080p low. Sunshine's X11 capture is SDR only (HDR
needs KMS capture).

### Desktop access boundary

Raw VNC binds only to loopback. Both the web desktop and audio WebSocket
proxy independently validate the browser's Authelia session, using the fixed
Steam hostname and requiring an authenticated identity. Forged identity headers,
missing cookies and authentication outages fail closed. Use the HTTPS ingress;
direct node access no longer provides an anonymous desktop. Desktop sudo access is
removed and root password login is locked; use Kubernetes exec for administration.

### Validation (2026-10-21)

Verified AMD OpenGL acceleration, Sunshine H.264/HEVC VAAPI encoders, Xorg's three
virtual desktop inputs, synthetic controller hotplug and SDL button delivery,
Bubblewrap with the existing proc mount, and the absence of physical input nodes.
NAS, remote Kubernetes API and metadata connections fail; public HTTPS and
Authelia work. From a separate LAN node, streaming TCP ports respond and raw
8083/47990 do not. An unauthenticated local desktop request returns 401.

The persisted Steam installation has not completed its initial installer prompt;
no installed game or actual Moonlight client session was exercised. Complete
Steam setup through the desktop and verify a game from the intended client.
