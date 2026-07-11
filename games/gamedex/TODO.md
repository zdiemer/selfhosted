# Gamedex — in flight

Working list. Checked = shipped and deployed.

## Done

- [x] **Reviews reader** — 722 reviews / 83,500 words, own tab, full-text search
      with hit highlighting, sort by date/rating/length, expand in place.
- [x] **Launch game (Steam)** — `steam://rungameid/<appid>` on 3,637 owned Steam
      games; a "View on Steam" store link on the rest of the 5,928 we know an
      appid for. IGDB renamed `category` → `external_game_source` (the old name
      silently returns nothing); source 1 = Steam.
- [x] **Data health tab** — 14 checks ported from GamesMaster/GamePicker. 58
      possible duplicates · 451 completed with no date · 724 with no completion
      time · 1,185 owned with no price · 4 wishlisted-and-owned · 14 playtimes
      wildly off HLTB. Rows are clickable; fix in Dropbox, they clear on the
      next poll.
- [x] **Year in review + burn-down** — year picker; 107 completions/yr means the
      13,078-game backlog runs out in ~123 years (2149).
- [x] **Stats: varied charts + imagery** — cumulative area chart, calendar
      heatmap, you-vs-critics scatter (every dot a clickable game), genre radar,
      cover-art poster walls.
- [x] **PWA** — manifest + service worker. Installable; shell is cache-first,
      data is network-first with a cached fallback, covers are cache-first.
- [x] **Collection value over time** — daily snapshot, weekly GameEye re-scrape
      (a cached price is a stale price). First point: $55,837.69 across 1,884
      priced games.

## Next

- [ ] **Launch: more platforms** — RomM deep-links for emulated titles (we
      already run RomM), plus GOG / Epic / itch handoffs. IGDB gives us those
      storefront ids already (`external_game_source` 5 = GOG, 26 = Epic,
      15 = itch); only Steam is wired up so far.

## Bugs (fixed)

- [x] Home page image flicker — the hero rotation and the enrichment poller both
      called `renderHome()`, rebuilding every `<img>`. Hero redraws alone now.
- [x] Home hero unpageable on mobile — added swipe + arrows, bigger dots.
- [x] Review covers stayed placeholders — the tab renders before enrichment
      lands and `patchEnrichedCells` only touches the grid/table.
- [x] Health "no metadata" reported all 14,747 games — results were cached
      before the enrichment map arrived.

## Notes

- Deploy = bump the tag in Chart.yaml/values.yaml (and `sw.js`'s VERSION, which
  keys the cache), `docker build/push`, `bash upgrade.sh`.
