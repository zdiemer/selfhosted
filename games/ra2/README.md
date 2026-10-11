# Red Alert 2 / Yuri's Revenge WASM recompilation

Self-hosts the owner's [zdiemer/redalert2-recomp](https://github.com/zdiemer/redalert2-recomp)
WebAssembly targets, pinned in `values.yaml`. Both are static recompilations of
the original game binaries, using pcrecomp's Win32 HLE and the project's own
browser launcher.

- https://ra2.zachd.duckdns.org — `game` / Red Alert 2
- https://yuri.zachd.duckdns.org — `gamemd` / Yuri's Revenge

Connect to Tailscale first. The private DuckDNS tier resolves to tailnet node
addresses, uses the shared wildcard certificate, and has no Cloudflare tunnel
hostname. This follows the existing closed WAN ingress configuration. Upgrade
refuses DNS outside the tailnet range. Do not publish these hosts externally:
WASM builds contain code derived from the owner's retail binaries.

## Build

```bash
./build.sh
./upgrade.sh
```

Docker builds both targets from the NAS installation using the project's
`tools/wasm/bootstrap.py` and `tools/wasm/build.py`: pinned Emscripten, pcrecomp
and FFmpeg, disassembly, lift, then compilation. Each target has its own
output directory. Allow about 90 minutes for a first build on the four-core builder; later
builds reuse Docker's cached stages. Compiler concurrency is three to bound
memory use.

Only `game.exe` and `gamemd.exe` are copied to a temporary named build context
outside the repository. The final image contains the two WASM builds and a
Liberation Sans font; runtime assets are mounted separately. Images are pushed
only to the private cluster registry. Never commit retail inputs, generated C,
analysis catalogs, WASM output, or browser caches to this public repository.

Update `source.commit`, `image.tag`, and Chart.yaml `appVersion` together when
upgrading. The build pins the project commit; upstream bootstrap pins its own
toolchain revisions. No project code is patched. FFmpeg's LGPL source and
license are served at `/ffmpeg-source.tar.gz` and `/FFmpeg-LICENSE`.

## Runtime files

The existing `games/romm-library` read-only SMB claim mounts only
`Roms/_steam/Command and Conquer Red Alert II`. `nas.buildPath` is the same
installation's local mount for image builds. An init container reproduces the
file selection from upstream `tools/wasm/serve.py` into per-target JSON manifests
and symlinks. nginx serves those manifests, font, game files and build output,
with the COOP/COEP headers needed for WASM pthreads.

The NAS originals are never changed. `/` redirects to the original launcher at
`/ra2.html?skipintro=1`, which changes the INI only in browser memory to disable
the long opening intro. Campaign cutscenes remain available. First load reads
large archives into browser memory, so allow time and use a desktop browser with
sufficient memory (upstream estimates about 1 GiB for RA2, 1.8 GiB for Yuri).
The required movie MIX archives must also be present.

Browser input and fullscreen use upstream controls: click the canvas to capture
the pointer and enable audio, Escape releases capture/skips cutscenes, F11 toggles
fullscreen. The current launcher uses an in-memory filesystem: do not rely on
saves surviving a reload. Native UDP multiplayer is unavailable in the browser;
there is no relay in this deployment.

## Validation

Upstream's `tools/wasm/playtest.py` supports `menu`, `video`, `skirmish`, and
`campaign` checks against each HTTPS hostname. Use `/ra2.html` as its `--url`
and `--target game` or `--target gamemd`. The skirmish check requires advancing
simulation frames and real browser selection/deployment input, rather than
only a rendered menu. Retain test artifacts outside this repository.

Deployment validation on 2026-10-10: both targets reached 1920×1080 skirmishes
with advancing simulation, audio, pointer capture, movie playback, and zero
JavaScript errors. RA2 passed all upstream checks. Yuri's automated browser
selection/deployment check failed, including a retry with explicit pointer
capture; interactive unit selection/deployment remains unverified for Yuri.
