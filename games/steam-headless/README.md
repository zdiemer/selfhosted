# steam-headless — Steam + Sunshine for Moonlight clients

Runs [Steam-Headless](https://github.com/Steam-Headless/docker-steam-headless)
on zachd-ubuntu-6 (Ryzen 7 6800H, Radeon 680M): Xorg + XFCE + Steam on the
real GPU, with [Sunshine](https://github.com/LizardByte/Sunshine) capturing it
and encoding H.264/HEVC on VAAPI (the 680M has no AV1 encoder). Clients are
Moonlight — "Moonlight Game Streaming" on iOS/Apple TV, plus Android, desktop
and Steam Deck builds.

Deploy: `./upgrade.sh` (secrets from `op://homelab/games-steam-headless`).

## Addresses (tailnet only)

| What | Where |
|---|---|
| Moonlight host | `zachd-ubuntu-6.taila58f7e.ts.net` (`100.86.22.101`) — works home or away; `192.168.4.22` also works on the LAN |
| Sunshine web UI (PIN entry, settings) | `https://sunshine.zachd.duckdns.org` — Authelia, then `admin` / `sunshine.password` |
| noVNC desktop (Steam login, library setup) | `https://steam.zachd.duckdns.org` — Authelia |

The two web UIs go through Traefik (`templates/ingress.yaml`). Moonlight's
stream can't: it is fixed-port UDP straight to the encoding host, which is why
the Moonlight host is ubuntu-6's own Tailscale name rather than a duckdns name
(those all resolve to whichever node infra/duckdns picked). Sunshine's ports
(TCP 47984/47989/47990/48010, UDP 47998-48000/48002/48010) and noVNC's 8083
still bind on the node itself (`hostNetwork`), so the raw LAN addresses keep
working — and `http://192.168.4.22:8083` skips Authelia entirely.

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
- **No host `/dev` mounts, forced dumb-udev.** With `hostNetwork` the image's
  udevd receives the host's uevents; with the host's `/dev/dri` bind-mounted it
  deleted `card1` and regrouped `renderD128` from 992 to 993 on the host,
  which breaks Jellyfin's VAAPI on this node. Containerd already gives a
  privileged container private copies of the host's device nodes; see
  `templates/configmap-init.yaml`.
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
