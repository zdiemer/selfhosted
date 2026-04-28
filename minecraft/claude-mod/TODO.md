# claude-mod / claude-bridge — open ideas

Forward-looking enhancements that surface more game state to the
in-game `/claude` integration. Each item is independently shippable
in its own commit; group order is rough priority, not blocking.

## Tier 2 — single-purpose mod-API integrations

Each adds one mod's API as a `compileOnly` dep (synced from `/data/mods`
by `install.sh`'s `prep_libs()`) plus one new `/claudemod query`
subcommand. The bridge's `run_query_command` allowlist already covers
`^claudemod\s+query\b`, so the bridge changes are typically just a
system-prompt nudge.

## Tier 3 — bigger / cross-system

## Tier 4 (potential, unscoped)

- **Vanilla advancements** — read `world/advancements/<uuid>.json`
  and surface progress per category. Complementary to FTB Quests for
  packs that lean on both.
- **Combat / weapon state** — `bettercombat-fabric` + `combatroll`
  + `ranged_weapon_api` track per-player roll cooldowns, weapon
  combo state, etc. Niche but potentially fun.
- **Villager trades** — `claudemod query trades <x> <y> <z>` reads
  the villager block entity and dumps offers. "Best deal at the
  librarian?" given a coord.
- **BlueMap marker enhancements** — colored / shaped markers,
  per-player marker visibility, marker categories (claim / dungeon
  / waypoint).

## Conventions when adding a new query

1. Read-only — gate via `requires(src -> !src.isExecutedByPlayer())`
   so only RCON / console can invoke; the bridge's allowlist already
   covers `^claudemod\s+query\b`.
2. JSON output via `sendFeedback` with the helpers in
   `ClaudeQueryCommand.java` (`reply()` / `error()`); cap at ~3500
   chars to fit RCON.
3. If the query needs a mod's API, add the jar to the prefix list in
   `install.sh::prep_libs()` and reference it via `modCompileOnly
   fileTree`. Don't bundle.
4. After adding the subcommand, update the bridge system prompt in
   `minecraft/claude-bridge/values.yaml` with a one-line example so
   Claude knows when to reach for it.
