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
  franchise, genre, year, format, status, and boolean toggles), full-text
  search, a sortable/paginated table, and a click-through detail drawer.

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
- `src/app.py` — FastAPI: `/api/data`, `/api/health`, static UI.
- `static/` — `index.html`, `app.js`, `style.css` (no build step).

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
# namespace already exists (shared with romm); create it if not:
#   kubectl create namespace games

cp games/gamedex/values.local.yaml.example games/gamedex/values.local.yaml
$EDITOR games/gamedex/values.local.yaml         # paste the Dropbox link

bash games/gamedex/build.sh                      # build + side-load image into k3s
bash games/gamedex/upgrade.sh                    # helm upgrade --install + rollout
```

Then browse **https://games.zachd.duckdns.org**. First paint waits a few seconds
while the pod pulls the workbook (the UI shows "Fetching spreadsheet from
Dropbox…"). `/api/health` returns `503` until that first load completes.

## Upgrading

- Changed **app code / Dockerfile / static assets** → `./build.sh` then
  `./upgrade.sh` (the image tag is `IfNotPresent`, so rebuild + re-import first).
- Changed **only chart values** (e.g. the Dropbox link, refresh interval) →
  `./upgrade.sh` alone.
- Bump `image.tag` in `values.yaml` (and `Chart.yaml` `appVersion`) when you
  want a clean new image tag rather than overwriting the current one.

## Configuration (`values.yaml` / `values.local.yaml`)

| Key | Where | Default | Notes |
|---|---|---|---|
| `dropbox.url` | local | — (required) | Shared folder (or file) link |
| `dropbox.filename` | either | `Games Master List - Final.xlsx` | Workbook name inside a shared folder |
| `refreshIntervalSeconds` | either | `600` | Poll cadence; re-parses only on change |
| `ingress.host` | either | `games.zachd.duckdns.org` | Public hostname |
| `image.tag` | either | `0.1.0` | Must match the tag `build.sh` imports |

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
