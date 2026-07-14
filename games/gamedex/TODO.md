# Gamedex — in flight

Working list. Checked = shipped and deployed.

## Done

- [x] **Reviews reader** — 722 reviews / 83,500 words, own tab, full-text search
      with hit highlighting, sort by date/rating/length, expand in place.
- [x] **Launch game (Steam)** — `steam://rungameid/<appid>` on 3,637 owned Steam
      games; a "View on Steam" store link on the rest of the 5,928 we know an
      appid for. IGDB renamed `category` → `external_game_source` (the old name
      silently returns nothing); source 1 = Steam.
- [x] **Data health tab** — 19 checks ported from GamesMaster/GamePicker (14 at
      first; match-confidence, misspelling, sequel-mismatch, incomplete-collection
      and no-priority came later). 58 possible duplicates · 451 completed with no
      date · 724 with no completion time · 1,185 owned with no price · 4
      wishlisted-and-owned · 14 playtimes wildly off HLTB. Rows are clickable; fix
      in Dropbox, they clear on the next poll.
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

- [x] **Launch: more platforms** — the Notes column picks the storefront (it
      says which copy you own), IGDB supplies the id. 4,001 real launch buttons
      (Steam 3,880 · GOG 121) plus store/app links for Epic, itch, PlayStation,
      Xbox, Google Play and the App Store (iOS works: apps.apple.com/id<appid>).

- [x] **Trailers + artwork** — IGDB's videos and artworks were fetched and never
      shown. Click-to-play trailers, artworks folded into the gallery, and the
      hero backdrop now prefers key art over a screenshot.
- [x] **Steam extras** — Deck compatibility (Valve), ProtonDB tier, SteamSpy
      owners/reviews, achievement rates. Keyed on the appid: cannot mismatch.
      New facets: Steam Deck, ProtonDB, Steam reviews.
- [x] **speedrun.com + StrategyWiki** — world records and walkthrough links.

- [x] **Recommendations** — "because you liked …" from IGDB's similar_games
      (stored since day one, never used), IDF-weighted and filtered for
      relatedness. Plus a predicted rating: ridge regression trained in-browser
      on your 1,707 rated games. MAE 9.7pts vs 10.8 for quoting Metacritic.

- [x] **The metadata we were already paying for, again** — six sources were storing
      fields nobody ever read. The arcade cabinet's own shortplay VIDEO (every
      machine had one). VNDB's synopsis — the only description a visual novel IGDB
      never matched has. GameEye's Manual-only and Box-only prices, which are what
      the gap between loose and CIB is actually made of. Co-Optimus's prose on what
      the co-op is LIKE. The link to the record RUN, not just the leaderboard. The
      rarest achievement's NAME (we printed "rarest 0.4%" and never said of what).
      And `manuals` + `gametdb` were never returned by /api/enrichment/detail at
      all, so the booklet's page count and the printed disc face were unreachable.
      New "In the box" drawer section; GameTDB box fronts and VGChartz box scans
      now fill the cover chain and the Shelf, so a disc IGDB never matched gets a
      real, region-correct box instead of a grey slab.

## Next

- [ ] **New sources** — investigated but not yet chosen. Candidates worth verifying
      properly (API terms, rate limits, and above all whether a BULK DUMP exists,
      because 14k per-game scrapes is the thing to avoid): RetroAchievements (428
      owned games sit on RA-class platforms), PriceCharting (alongside GameEye),
      Wikidata SPARQL (free bulk cross-IDs, one query for thousands of rows),
      MobyGames (staff credits — nothing we have covers who MADE a game),
      OpenCritic, PCGamingWiki. Nothing here is confirmed yet.

- [ ] **Match confidence for SECONDARY sources** — HLTB, Metacritic, GameEye, VNDB,
      VGChartz, speedrun and guides all compute MatchValidator.match_score and then
      drop it on the floor; only the primary IGDB match persists a score (now shown
      in Health). Recording theirs means a `score` column per source table and a
      re-match of everything already cached — ~14k games x 7 sources of rate-limited
      scraping — so it can't be backfilled cheaply. Cheap half-step: store it for
      new/refreshed matches, and show "not recorded" for the rest.

- [ ] **Launch: RomM** — BLOCKED. We do not actually run RomM: `games/romm/` is a
      chart with no Helm release and no pod, and its README lists manual NAS/SMB
      prerequisites that were never done. Deep-linking emulated titles needs the
      instance up first (and then a lookup by name — RomM isn't an IGDB storefront,
      so there's no id to build a URI from).
- [ ] **Launch: EA / Ubisoft / Battle.net** — not currently possible: IGDB has no
      external_game_source for any of them, so there's no offer/app id to build a
      URI from. Would need another source (or hand-maintained ids).

## Bugs (fixed)

- [x] **The PWA had no offline cache for sixteen releases** — `sw.js` still listed
      `./reviews.js` in `SHELL_URLS`, and that file was deleted back in 1.11.4 when
      the Reviews tab was folded into the timeline. `cache.addAll()` is atomic, so
      one 404 rejected the whole batch, the install handler threw, and the service
      worker never cached anything. Silent, because online everything still worked.
      `shelf.js` was missing from the list too. Now each URL is cached on its own,
      so a stale entry can never take the whole cache down again.
- [x] Permanent loading skeleton on a game with no metadata — fixed by `NO_MATCH`:
      "still looking" and "looked, found nothing" are now distinguishable, so a row
      that will never have a cover settles instead of shimmering forever.
- [x] `gametdb.py` logged `console` in an exception handler, but the parameter is
      `dump` — a malformed dump raised NameError *from inside the error handler*,
      turning a warning into a crash that killed the whole refresh.
- [x] `manuals.py` had a loop whose entire body was `continue`, so the page count was
      always None; `pdf`, `pages` and `collections` were computed and never persisted.
      The booklet now carries its page count and a direct PDF link.
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
