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

### Explorer's Compass / Nature's Compass — nearest biome / structure
- New: `claudemod query nearest biome <id> [<player>]`
- New: `claudemod query nearest structure <id> [<player>]`
- Both mods are installed (`ExplorersCompass-1.20.1-2.2.3-fabric.jar`,
  `NaturesCompass-1.20.1-2.2.3-fabric.jar`) and ship public APIs that
  scan a region offset from a position for the matching biome/structure.
- Adventurez registers its boss arenas as named structures, so this
  likely answers "where do I find the lich king?" better than the
  current quest-text search.
- Defaults to the asking player's current dimension + position; bridge
  resolves caller pos via `data get entity`.

### Open Parties and Claims — claims + party
- New: `claudemod query opac <player>`
- Mod installed: `open-parties-and-claims-fabric-1.20.1-0.25.10.jar`.
- Surface: claimed chunk count, bounding box per claim, party name +
  members, party permissions for the asking player.
- Answers "how many chunks have I claimed?", "is StarFoxA in my party?",
  "who can build in my base?".

### Spell Engine — known spells + mana
- New: `claudemod query spells <player>`
- Mods: `spell_engine`, `spellbladenext`, `spell_power`, plus several
  Spell Engine add-ons (`extraspellattributes`, `archers-expansion`,
  `invoke`).
- Surface: equipped spellbook contents, currently-known spells, mana
  current/max, mana regen rate, school affinities if exposed.

### Travelers Backpack — equipped backpack contents
- New: `claudemod query backpack <player>`
- Mod installed: `travelersbackpack-fabric-1.20.1-9.1.50.jar`.
- The currently-equipped backpack stores its inventory in an attached
  entity invisible to `claudemod query inventory`. Surface contents
  (slots), tank fluid levels, and tool slots.

## Tier 3 — bigger / cross-system

### Combined gear stats (resolved item attributes)
- New: `claudemod query gear <player>`
- For held item + each armor slot: compute final attack / defense /
  crit / movement / etc. AFTER all attribute modifiers resolve —
  including Apotheosis / Zenith affix data, gem sockets, and Custom
  Item Attributes.
- Walks the attribute modifier list per slot and aggregates by
  attribute name. Distinct from `query vitals`, which dumps the
  player-level resolved values; `gear` breaks down WHICH item /
  affix contributes WHAT.
- Answers "is this new sword better than my current?", "what's my
  best armor piece?", "where's my crit chance coming from?".

### Server perf
- New: `claudemod query perf`
- TPS over 1m / 5m / 15m windows, mean MSPT, total entities loaded,
  total chunks loaded, top-N busiest dimensions. Uses Fabric's
  `TickManager` plus a small ring buffer in the mod for the rolling
  windows.
- Spark monitoring is already running externally (cluster CronJob,
  see `minecraft/monitor.sh`), but exposing inline lets Claude
  answer "is the server lagging?", "what's eating CPU?" without
  the operator pulling Grafana.

### Unified class / skill summary
- New: `claudemod query class <player>`
- Consolidates state from puffish_skills + simplyskills +
  more_rpg_classes into one view: per-system level + chosen class +
  key stats + unspent points. Replaces the need for Claude to call
  three siloed queries to answer "what's my build?".
- Drop-in extension to the existing `claudemod query skills` —
  could either supersede it or live alongside.

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
