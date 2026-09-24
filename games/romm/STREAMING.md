# Native emulator streaming

Enabled September 21, 2026 with RomM 5.3.0 and Webstation broker 0.8.5.

## Use

Refresh RomM, open a supported game, and choose **Stream**. The browser player
remains available for systems that support both. Admins can open a desktop from
the streaming fleet to adjust emulator settings. One Webstation container means
one active game or desktop session at a time, shared across all platforms.

The desktop runs on `zachd-ubuntu-6` (Ryzen 7 6800H / Radeon 680M). It mounts
`/dev/dri`; startup confirmed GPU rendering and H.264/H.265 VAAPI encoding.
The image is pinned by digest in `values.yaml`. Both existing RomM hosts route
`/streaming/` to Webstation, retaining the prefix and their existing access
controls: Authelia on the public host, tailnet access on DuckDNS. Broker session
tokens protect stream connections. The broker API also requires its own secret.

The dedicated `streaming.brokerSecret` is in the existing `games-romm` 1Password
entry. Helm injects it as `BROKER_SECRET` in Webstation and
`STREAMING_BROKER_SECRET` in RomM. No plaintext secret is stored in this repo.

## Library and indexing

The chart configures 56 streaming platform slugs across native emulators and
RetroArch, alongside the existing browser-only systems. There are 69 filesystem
platform folders, because some folders share a platform slug.

Both pods build the same symlink tree, with the read-only SMB library at
`/romm/library/.nas`. `library_path` stays `/romm/library`; this is important for
ROM and save identity. The source NAS is never rewritten.

- GameCube/Wii use RVZ; Dreamcast uses CHD; PS2 uses ISO/BIN and supported
  compressed formats. Auxiliary cue sheets and Xbox compressed duplicate copies
  are omitted from the new top-level catalog entries.
- Nintendo 3DS and New Nintendo 3DS use their `_decrypted` CXI folders.
- Wii U uses `_decrypted` base-game directories (`00050000*`), omitting update
  and DLC directories. 3DS firmware, explicitly marked DLC and update entries
  are also excluded.
- Old platform exclusions remain for systems without a configured player.
- The first indexing pass skipped hashing and metadata. IGDB and Hasheous
  matching are now enabled for new imports; both provider health checks passed.
  File hashing is enabled for Hasheous. Existing unchanged hashes are cached.
  Large disc collections can take days to hash over SMB.
- RVZ/GCZ/WBFS and other opaque compressed disc formats use IGDB filename
  matching; RomM 5.3.0 does not derive their original disc hashes. The backfill
  skips those files. Dreamcast CHDs expose a lookup SHA-1 in their headers.
  RomM excludes platforms such as PS3/PS4, Switch, Wii U and Xbox 360 from
  ordinary file hashing. Hasheous matches are limited by its database coverage.

The initial discovery found 195,517 candidates, including already indexed games.
The 24 new platform folders are listed in `match-streaming.py`. Work runs through
RomM's serial RQ scan worker and persists in its Valkey volume. Watch the task
queue in RomM for progress and failures.

`index-streaming.py` queues missing folders, with IGDB and Hasheous selected.
It refuses to duplicate an existing library scan. Pass folder names explicitly
to resume partial indexing. Existing entries keep their metadata; Quick scans
only enrich newly discovered entries.

`match-streaming.py` queues **Unmatched** scans using both providers, appending
them after existing queued work. Run this for entries from the original hashless
index. Then run it with `--hashes` to backfill eligible, already indexed games
without a Hasheous ID in batches of 100. Those selected-ROM scans calculate
missing hashes, reuse unchanged ones, and preserve existing provider matches.
The hash plan is a snapshot of the catalog at invocation: run after discovery
finishes to include every entry, or use normal indexing with hashing enabled
for the remaining new entries. Stable job IDs prevent duplicate submissions
while their RQ records remain; inspect failed jobs before retrying.

Run the scripts inside the RomM container from `/backend` with its Python:

```bash
# POD must be the application pod, not the database or Webstation pod.
kubectl -n games exec -i "$POD" -c romm -- \
  sh -c 'cd /backend && /src/.venv/bin/python -' \
  < games/romm/match-streaming.py

# Preview hash batches; omit --dry-run to enqueue.
kubectl -n games exec -i "$POD" -c romm -- \
  sh -c 'cd /backend && /src/.venv/bin/python - --hashes --dry-run' \
  < games/romm/match-streaming.py
```

IGDB matches may still need manual correction for ambiguous filenames. A
completed scan does not mean every game has a provider match.

September 21 backfill: 24 metadata scans, 19 resumed index jobs using both
providers, and 139 hash batches covering 13,571 previously indexed games were
submitted. GameCube matching was confirmed progressing. Live selected-ROM
checks stored CRC/MD5/SHA-1 for Mega Man 9 (WiiWare) and a CHD lookup SHA-1 for
18 Wheeler (Dreamcast); both received IGDB metadata and covers. Hasheous was
reachable but returned no match for those two dumps.

## Firmware, saves and persistence

Webstation has a 100 GiB `truenas-iscsi` PVC mounted at `/config`, included in
normal games backups. It holds emulator settings, firmware installation, game
caches, and working saves. RomM synchronizes session saves and states back into
its own save library. GameCube and PS2 use whole memory-card synchronization.

`files/setup-webstation.py` runs at each container initialization and preserves
existing settings. It prepares:

- RetroArch BIOS links, PS1 BIOS paths, Dreamcast boot/flash files.
- PCSX2 BIOS selection and hardware OpenGL rendering; the broker manages folder cards.
  The pinned image lacks `libshaderc.so.1`, so its default Vulkan path fails.
  The official PCSX2 `patches.zip` is stored on the PVC and linked into the
  image resources at startup (SHA-256
  `36cd0df92330638433d205fa58a44d5cf3a497d3fbbc45d8aa886d6a21fada48`).
  On a fresh PVC, obtain it from the PCSX2/pcsx2_patches GitHub release.
- Azahar seed database, Eden keys and installed Switch firmware from the NAS.
- A writable copy of the Xbox HDD template, separate from the read-only NAS.

PS3 firmware 4.93 was installed from `PS3/_bios/PS3UPDAT.PUP` into RPCS3's
persistent `dev_flash`. On a fresh PVC it must be installed again, preferably
through the admin desktop. The headless installer successfully installed it but
aborted during process teardown; the installed files were verified afterward.
Use `/opt/rpcs3/AppRun` rather than the image's broken `/usr/bin/rpcs3` symlink.
The broker already uses the correct path.

Enabling a platform is not a per-game compatibility guarantee. PS3 images need
the format/decryption RPCS3 expects; PS4 and newer consoles have substantial
compatibility and performance limits. Firmware/controller choices may still
need adjustment for specific games. GameCube/Wii and PS2 are the first targets
for this host; Wii motion controls depend on the chosen game and controller.

## Verification

- Helm config/routing validation and live RomM/Webstation rollouts.
- Broker health returns 200; public HTTPS path redirects unauthenticated users
  to Authelia; tailnet HTTPS path reaches the broker.
- Admin desktop claimed and released through RomM.
- Mario Kart: Double Dash launched through RomM into Dolphin. A captured frame
  showed the game's intro rendering correctly.
- RomM Save & Exit reported `saved: true` and `released: true`, with a state
  recorded in its database. Restoring that state through RomM returned an active
  Dolphin session; the verification session was then released.

- Katamari Damacy booted through the PCSX2 broker with hardware OpenGL and
  reached the in-game memory-card prompt. The test session was closed afterward.

Browser gamepad/audio latency and sustained frame pacing still need a play test
from the user's device; a server-side screenshot does not establish those.

Sources:

- [RomM 5.3.0](https://github.com/rommapp/romm/releases/tag/5.3.0)
- [Pinned compose example](https://github.com/rommapp/romm/blob/5.3.0/docker-compose.streaming.yml)
- [Broker and emulator documentation](https://github.com/romm-streaming/romm-broker)
- [Webstation image](https://github.com/linuxserver/docker-webstation/tree/romm)
