# Gamedex — in flight

Working list. Checked = shipped and deployed.

## Now

- [x] **Reviews reader** — shipped. 722 reviews / 83,500 words, own tab, search
      with hit highlighting, sort by date/score/length, expand in place.
- [x] **Launch game (Steam)** — shipped. `steam://rungameid/<appid>` on 3,637
      owned Steam games; a "View on Steam" store link on the rest of the 5,928 we
      know an appid for. IGDB renamed `category` to `external_game_source` (the
      old name silently returns nothing); source 1 = Steam.
- [ ] **Launch: more platforms** — RomM deep-links for emulated titles (we run
      RomM already), GOG / Epic / itch handoffs.
- [x] **Data health tab** — shipped. 14 checks; 58 duplicates, 451 completed
      with no date, 724 with no time, 1,185 owned with no price, 4 wishlisted+
      owned. Clickable rows open the game.
- [x] **Stats: more varied charts + imagery** — shipped. Cumulative area chart,
      calendar heatmap (370 day cells), scatter of you-vs-critics (1,232 dots,
      each clickable), genre radar, and cover-art poster walls. — port the validators from GamesMaster/GamePicker.
      Known counts: 24 potential duplicates · 724 completed with no completion
      time · 451 completed with no date · 304 unknown playability · 1,185 owned
      with no purchase price · 23 started-but-never-finished. Plus HLTB
      mismatches and big me-vs-critic gaps.
- [x] **Year in review** — shipped, with the burn-down: 107 games/yr, 13,078
      backlog, ~123 years, i.e. the year 2149.
- [ ] ~~Year in review~~ — per-year recap (finished, hours, best/worst, longest,
      genre mix, vs prior years) with a year picker, plus the backlog burn-down
      projection: at 162 completions/yr the 13,078-game backlog takes ~81 years.
- [ ] **PWA** — manifest + service worker: installable on the phone, offline
      access to the last-loaded data.
- [ ] **Collection value over time** — snapshot the GameEye-derived total into
      SQLite daily so the $55,866 collection value can be charted as a trend.
      Worthless today, valuable in six months — so start recording now.

## Bugs

- [x] Home page image flicker — the 9s hero rotation and the enrichment poller
      both called `renderHome()`, which rebuilds every `<img>` on the page. Now
      the hero redraws alone and covers are patched in place.
- [x] Home hero can't be paged on mobile — added swipe + always-visible arrows,
      and made the dots a real tap target.
- [x] Reviews covers stayed as placeholders — the tab renders before the
      enrichment map loads and `patchEnrichedCells` only touches the grid/table.

## Notes

- Ideas are sized against real data; the counts above come from the live sheet.
- Deploy = bump the chart + values tag, `docker build/push`, `bash upgrade.sh`.
