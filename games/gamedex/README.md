# gamedex

A tiny, unauthenticated webapp that mirrors the **Games Master List** workbook
from Dropbox and makes it searchable with facets. Whenever the spreadsheet is
edited on any machine, the change flows through to the site on the next poll.

- **Source:** a Dropbox **shared link** (folder or file). The app polls it on an
  interval and re-parses only when the file content changes.
- **Sheets:** `Games` (played + backlog), `Finished Games` (completed, with the
  long-form reviews), and `Games On Order` (preorders).
- **Privacy:** the order sheet's **Address on Order**, **Order #**, and
  **Tracking #** columns are stripped server-side and never sent to the browser.
- **UI:** three tabs, a faceted sidebar (platform, region, publisher, developer,
  franchise, genre, year, format, status, boolean toggles, and — with IGDB —
  genre/theme/game-mode), full-text search, **table or cover-grid views**, a
  page-size selector (25–500), multi-key sorting, and a click-through detail
  drawer with rich IGDB metadata.

Served publicly at **https://games.zachd.duckdns.org** via Traefik + DuckDNS,
matching the ingress pattern used by the rest of the cluster.

## How it works

```
Dropbox shared link ──poll(600s)──▶ DataStore (in-memory)
   folder → zip, extract xlsx           │
   file   → xlsx bytes                  ▼
        sha256 diff → re-parse   FastAPI  ── /api/data (gzipped JSON) ─▶ static UI
                                          ── /api/health (503 until first load)
```

There is **no database and no PVC** — the whole dataset (~17k rows) lives in
memory and is re-fetched on boot and every `refreshIntervalSeconds`, so a pod
restart self-heals. Faceting and search run client-side over the JSON payload
(~1 MB gzipped), which is plenty fast at this scale.

Key files:
- `src/parse.py` — xlsx → normalized JSON (Excel-serial dates, 0–1 ratings, 0/1
  booleans, decimal-hour times; PII stripped). Column schemas live here.
- `src/poller.py` — Dropbox fetch loop, folder-zip/file detection, hash diffing.
- `src/app.py` — FastAPI: `/api/data`, `/api/health`, enrichment endpoints, UI.
- `src/igdb.py` — IGDB client (Twitch auth, one nested request per title) + matcher.
- `src/enrich.py` — lazy, host-cached enrichment (SQLite on the PVC).
- `src/pcgamingwiki.py`, `src/wikidata.py` — the two **exact-join, bulk** sources. They
  do no per-game fetching at all: each pulls its whole dataset once (Cargo API / SPARQL),
  caches it on the PVC, and every lookup is a dict hit. PCGamingWiki joins on the Steam
  appid (ultrawide, 4K, HDR, ray tracing, D3D/Vulkan, 64-bit, controller); Wikidata joins
  on the IGDB slug and brings the composer, the director, a Wikipedia article and a
  MobyGames id. Because both key on an id rather than a title, neither can mismatch.
- `src/match_validator.py`, `src/constants.py`, `src/excel_game.py` — title
  matcher ported near-verbatim from [zdiemer/GamesMaster](https://github.com/zdiemer/GamesMaster).
- `static/` — `index.html`, `app.js`, `style.css` (no build step).

## IGDB metadata enrichment

Set `igdb.clientId`/`igdb.clientSecret` (Twitch app, in `values.local.yaml`) to
enable cover art + rich metadata. It's **lazy and host-cached**:

- Only games you actually browse get matched — the frontend requests IGDB data
  for the ~50 rows on screen, they resolve at IGDB's 4 req/s limit (~12s/page),
  and results are cached in a SQLite file on the PVC (`/data`) **forever**.
- Matching reuses the GamesMaster `MatchValidator` (normalization, roman
  numerals, article/subtitle handling, platform aliases) scored against
  platform + release year. **Blank on low confidence** — an uncertain title is
  left un-enriched rather than shown a wrong cover.
- The detail drawer shows cover, IGDB rating, summary, genres/themes/modes,
  dev/publisher, screenshots, similar games, and an IGDB link (attribution
  required by IGDB's terms).

Optional **backfill** (`igdb.backfill: true`) slowly enriches *every* game in the
background to build a complete dataset — off by default to respect rate limits.

Endpoints: `POST /api/enrichment` (batch of matchKeys → light covers/facets),
`GET /api/enrichment/detail?key=` (full detail for the drawer),
`GET /api/enrichment/stats`. Each served row carries a `_k` matchKey.

## Local asset cache

The UI pulls a *lot* of assets from third parties — IGDB covers/screenshots/artwork
above all, plus GameTDB disc & box scans, VNDB covers, the Arcade Database's
cabinet/marquee art, the IGN/GameSpot fallback covers, and the Internet Archive's
PDF instruction manuals. Each is a fresh cross-origin round-trip that keeps a
skeleton shimmering until the bytes arrive, and it repeats for every visitor on
every device.

With `imageCache.enabled` / `manualCache.enabled` (both default on), the frontend
points every one of those requests at **`/api/img?u=<url>`** (images) or
**`/api/manual?u=<url>`** (PDF manuals) instead of the source server. The first
request fetches the asset and stores it under `/data/imgcache` or
`/data/manualcache`; every request after — from any browser — is a local read off
the PVC. Each cache is keyed by the sha256 of the source URL and **bounded**: once
it passes its `maxMb` it evicts the least-recently-served files, so it can't fill
the volume. The service worker keeps a browser-side copy on top of that, so a
repeat view costs neither a source fetch nor a round-trip to the pod.

The **manuals** were the slowest thing in the app: the drawer used to embed the
Archive's BookReader, which boots over the network every time you open a booklet.
When the Archive item has a PDF, the reader now pages through our cached copy in
the browser's own viewer — instant on a repeat open, and it works offline. Items
with no PDF still fall back to the BookReader embed.

Everything **fails open**: if a URL is unsafe, the source is down, or the body
isn't the type we expected, the endpoint 302s to the original so the asset still
loads — caching is an optimisation, never a gate. An **SSRF guard** only fetches
public http(s) hosts (private/loopback/link-local/metadata addresses are refused)
and only stores bytes that actually sniff to the advertised type (image, or PDF),
each under a per-item size cap.

**Not cached, on purpose:** YouTube thumbnails (already fast and Google-cached)
and the drawer videos (ArcadeDB shortplays, Thumby title cards) — those are few,
large, and would bloat the volume.

## Enlarging the volume

The PVC rides the k3s **local-path** provisioner, which is *not* a CSI driver, so
`allowVolumeExpansion` is `false` and an existing claim **cannot be resized in
place** — raising `persistence.size` and running `helm upgrade` would make the
upgrade *fail* on the PVC patch. Local-path also doesn't enforce a quota, so the
declared size is nominal: the pod can already use the node's free disk, and the
self-evicting caches keep themselves well within it. In other words you rarely
*need* to enlarge it — the caps do the bounding.

If you do want a bigger declared size, the only data-safe way is a one-time
**Retain → recreate** migration (brief downtime while the pod is down):

```bash
NS=games; PVC=gamedex-data
PV=$(kubectl -n $NS get pvc $PVC -o jsonpath='{.spec.volumeName}')

# 1. Keep the data if the PVC is deleted, and bump the PV's (nominal) capacity.
kubectl patch pv $PV -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
kubectl patch pv $PV -p '{"spec":{"capacity":{"storage":"10Gi"}}}'

# 2. Drop the pod so it releases the mount, then delete the PVC (data survives).
kubectl -n $NS scale deploy/gamedex --replicas=0
kubectl -n $NS delete pvc $PVC

# 3. Free the PV to bind again.
kubectl patch pv $PV --type=merge -p '{"spec":{"claimRef":null}}'   # -> Available

# 4. Set persistence.size: 10Gi in values, then recreate everything. The new
#    10Gi PVC binds to the now-Available 10Gi PV — same directory, data intact.
./upgrade.sh
```

Afterwards, optionally set the PV's reclaim policy back to `Delete`.

## Getting the Dropbox link

**Prefer a direct file link.** In Dropbox, right-click
`Games Master List - Final.xlsx` → **Copy link**. You'll get
`https://www.dropbox.com/scl/fi/<id>/Games-Master-List-Final.xlsx?rlkey=<key>&dl=0`.
Paste it into `values.local.yaml` as `dropbox.url`. The app rewrites
`dl=0`→`dl=1` and downloads only the ~2.5 MB workbook each poll.

A **folder link** (`…/scl/fo/…`) also works: the app downloads the folder as a
zip and extracts the workbook by name (set `dropbox.filename` if the folder
holds more than one `.xlsx`). It's simpler to share but pulls the *entire*
folder on every poll — for a large folder that's a lot of wasted bandwidth, so
the file link is preferred.

The link is unguessable but public — anyone with it can read the file. Since the
sensitive columns are stripped and the rest is low-sensitivity game data, that's
an acceptable trade for zero-OAuth setup.

## Install

```bash
kubectl create namespace games                   # once (shared with romm)

cp games/gamedex/values.local.yaml.example games/gamedex/values.local.yaml
$EDITOR games/gamedex/values.local.yaml          # paste the Dropbox link

docker login ghcr.io -u zdiemer                  # PAT with write:packages
bash games/gamedex/build.sh                      # build + push to ghcr.io/zdiemer/gamedex
# First push only: set the GHCR package to Public
#   https://github.com/users/zdiemer/packages/container/gamedex/settings
bash games/gamedex/upgrade.sh                     # helm upgrade --install + rollout
```

The cluster is multi-node with no in-cluster registry, so the image ships via
**GHCR** (a public package) rather than being side-loaded into each node's
containerd — that lets the single replica schedule onto any node and pull
anonymously.

Then browse **https://games.zachd.duckdns.org**. First paint waits a few seconds
while the pod pulls the workbook (the UI shows "Fetching spreadsheet from
Dropbox…"). `/api/health` returns `503` until that first load completes.

## Upgrading

- Changed **app code / Dockerfile / static assets** → bump `image.tag` in
  `values.yaml` (and `Chart.yaml` `appVersion`), then `./build.sh` (build +
  push to GHCR) and `./upgrade.sh`. Bumping the tag guarantees every node pulls
  the new image; reusing a tag can leave nodes on a cached layer.
- Changed **only chart values** (e.g. the Dropbox link, refresh interval) →
  `./upgrade.sh` alone.

## Configuration (`values.yaml` / `values.local.yaml`)

| Key | Where | Default | Notes |
|---|---|---|---|
| `dropbox.url` | local | — (required) | Shared folder (or file) link |
| `dropbox.filename` | either | `Games Master List - Final.xlsx` | Workbook name inside a shared folder |
| `refreshIntervalSeconds` | either | `600` | Poll cadence; re-parses only on change |
| `igdb.clientId` / `igdb.clientSecret` | local | — | Twitch app creds; enables enrichment |
| `igdb.backfill` | either | `false` | Slowly enrich every game in the background |
| `imageCache.enabled` | either | `true` | Cache hotlinked covers/screenshots/art on the PVC via `/api/img` |
| `imageCache.maxMb` | either | `500` | On-disk cap for the image cache; oldest-served files evicted past it |
| `manualCache.enabled` | either | `true` | Cache Internet Archive PDF manuals on the PVC via `/api/manual` |
| `manualCache.maxMb` | either | `1024` | On-disk cap for the manual cache; oldest-served files evicted past it |
| `persistence.size` | either | `1Gi` | PVC for the SQLite IGDB cache, shelf cuts, and asset caches (see *Enlarging the volume*) |
| `ingress.host` | either | `games.zachd.duckdns.org` | Public hostname |
| `image.tag` | either | `0.2.0` | GHCR image tag `build.sh` pushes |

## Troubleshooting

- **Stuck on "Fetching spreadsheet…"** — check the pod logs:
  `kubectl -n games logs deploy/gamedex`. A `poll failed:` line points at the
  cause (bad link, folder has no `.xlsx`, network). `/api/data` and
  `/api/health` report the last error in `meta.lastError`.
- **Edits not showing** — the poller only re-parses when the file's bytes change;
  wait up to `refreshIntervalSeconds`. Lower it temporarily to test.
- **A new spreadsheet column isn't typed right** — add it to the relevant schema
  list in `src/parse.py` (`_GAMES` / `_COMPLETED` / `_ON_ORDER`); unmapped
  columns pass through as plain text.
