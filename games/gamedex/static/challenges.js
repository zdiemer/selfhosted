"use strict";

/* "One per X" challenges — ported from zdiemer/GamePicker
   (src/game_selectors/progress/challenge_selectors.py).

   The rules, restated: each challenge slices the collection into buckets (one
   per platform, per genre, per letter of the alphabet, …). A bucket is CLEARED
   the moment any game in it is completed after the challenge's start date, so
   progress is derived entirely from the sheet's Date Completed column —
   there's nothing to tick off by hand. What's left to do is every bucket in
   the candidate pool that hasn't been cleared yet.

   Loaded after app.js and shares its globals (DATA, ENRICH, openDrawer, …). */

// GamePicker's CHALLENGE_START; individual challenges override it after a reset.
const CH_DEFAULT_START = "2024-10-20";
const chState = { open: null };

const chRows = () => ((DATA.sheets.games || {}).rows || []).filter((r) => r.title);
const chMean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;

// ---- ported scoring & eligibility ---------------------------------------

// ExcelGame.combined_rating: the mean of my own score (falling back to
// priority/5) and the mean of the external scores.
function combinedRating(r) {
  const others = [r.metacriticRating, r.gamefaqsUserRating].filter((v) => v != null);
  const mine = r.rating != null ? r.rating : (r.priority != null ? priorityRank(r.priority) / 5 : null);
  const parts = [];
  if (mine != null && !isNaN(mine)) parts.push(mine);
  if (others.length) parts.push(chMean(others));
  return parts.length ? chMean(parts) : null;
}

// ExcelFilter.is_playable_by_language: an untranslated game in a text-heavy
// genre isn't really playable, whatever the Playable column says.
const CH_TEXT_GENRES = new Set([
  "Action RPG", "Adventure", "Card Game", "Computer RPG", "Dungeon Crawler", "Strategy RPG",
  "Turn-Based RPG", "Visual Novel", "Action Adventure", "Turn-Based Strategy",
  "Turn-Based Tactics", "Strategy", "MMORPG", "Real-Time Tactics", "Roguelike", "Simulation",
  "Survival Horror", "Text Adventure", "Trivia",
]);

const chToday = () => new Date().toISOString().slice(0, 10);

// ExcelFilter's unplayed-candidate pool: not low priority, playable, playable
// in a language I read, unplayed, and actually out.
function isCandidate(r) {
  if (r.completed) return false;
  // Priority is a label now ("Might Play"), so Number() would be NaN and this
  // would reject every game in the library.
  if (!(priorityRank(r.priority) > 1)) return false;
  if (r.playable !== "Yes") return false;
  if (r.english === "None" && CH_TEXT_GENRES.has(r.genre)) return false;
  return !!r.releaseDate && r.releaseDate <= chToday();
}

// ---- Notes-derived fields ------------------------------------------------
// The sheet has no columns for storefront, subscription, limited-print run or
// required accessory — ExcelGame.__process_notes infers them all from the Notes
// cell, matching it against these closed vocabularies. Ported as-is, including
// the ordering: the first vocabulary that matches wins and the rest are skipped.

const CH_DIGITAL_PLATFORMS = new Set([
  "32-bit iOS", "Abandonware", "Amazon", "Battle.net", "Desura", "DRM Free", "Epic Games Store",
  "Freeware", "GOG", "Green Man Gaming", "Humble Bundle", "itch.io", "Johren", "Legacy Games",
  "Mojang", "Net Yaroze", "Nintendo 3DS Ambassador Program", "Oculus", "Origin", "Other",
  "Pirated", "Playdate", "Playdate Catalog", "Playdate Season 1", "Playdate Season 2",
  "Square Enix", "Steam", "Super NES Classic Edition", "Twitch", "uPlay", "Virtual Console",
  "Xbox Live Indie Games",
]);
const CH_SUBSCRIPTIONS = new Set([
  "Apple Arcade", "Games with Gold", "Netflix Games", "Nintendo Switch Online", "OnLive",
  "PlayStation Plus", "Stadia Pro", "Viveport", "Xbox Game Pass",
]);
const CH_LIMITED_PRINT = new Set([
  "Fangamer", "Hard Copy Games", "iam8bit", "Limited Rare Games", "Limited Run Games",
  "PixelHeart", "Play-Asia Exclusive", "Special Reserve Games", "Strictly Limited Games",
  "Super Rare Games",
]);
const CH_MEDIA_FORMATS = new Set(["LaserDisc"]);
const CH_ACCESSORIES = new Set([
  "Adventure Player", "Nintendo Power", "Starpath Supercharger", "Super Scope",
]);

const _chNotes = new WeakMap();
function chNoteFacts(r) {
  let f = _chNotes.get(r);
  if (f) return f;
  f = { notes: r.notes || null };
  const n = f.notes;
  if (n) {
    if (CH_DIGITAL_PLATFORMS.has(n)) f.digitalPlatform = n;
    else if (CH_SUBSCRIPTIONS.has(n)) f.subscription = n;
    else if (n === "Delisted") f.delisted = true;
    else if (CH_LIMITED_PRINT.has(n)) f.limitedPrint = n;
    else {
      // "Limited Run Games - Foo Edition": the company is stripped off and the
      // remainder falls through to the checks below.
      if (n.startsWith("Limited Run Games")) {
        f.limitedPrint = "Limited Run Games";
        f.notes = n.replace("Limited Run Games", "").replace(" - ", "").trim();
      }
      const rest = f.notes;
      if (CH_MEDIA_FORMATS.has(rest)) f.mediaFormat = rest;
      else if (CH_ACCESSORIES.has(rest)) f.accessory = rest;
    }
  }
  _chNotes.set(r, f);
  return f;
}

// ---- bucket keys ---------------------------------------------------------

// get_platform_completion_id: the platform, split by every distinction that
// makes a playthrough feel like a different box — Famicom vs NES, XBLA vs disc,
// Steam vs GOG, MAME vs not. Branch order is load-bearing (a Steam game is
// "PC (Steam)", never "PC"), so it follows the source exactly.
const CH_STOREFRONT = {
  "Xbox": "Digital", "Xbox 360": "XBLA", "Xbox One": "Digital", "Xbox Series X|S": "Digital",
  "PlayStation 3": "PSN", "PlayStation 4": "PSN", "PlayStation 5": "PSN",
  "PlayStation Vita": "PSN", "Nintendo 3DS": "eShop", "New Nintendo 3DS": "eShop",
  "Nintendo Wii U": "eShop", "Nintendo Switch": "eShop", "Nintendo Switch 2": "eShop",
};
const CH_VR_SPLIT = new Set(["PlayStation 4", "PlayStation 5"]);

function platformCompletionId(r) {
  const p = r.platform;
  if (!p) return null;
  const f = chNoteFacts(r);
  const notes = f.notes;

  // PC storefronts and Playdate sub-platforms.
  if (f.digitalPlatform) {
    return `${p} (${f.digitalPlatform})${r.vr ? " (VR)" : ""}${r.dlc ? " (DLC)" : ""}`;
  }
  if (r.dlc) return `${p} (DLC)`;
  if (p === "Arcade") {
    if (notes) return `${p} (${notes})`;                      // LaserDisc, Naomi, Triforce…
    return `${p} (${r.mameRomset ? "MAME" : "Non-MAME"})`;
  }
  if (p === "NES" && r.releaseRegion === "Japan") return `${p} (Famicom)`;
  if ((p === "NES" || p === "Game Boy Color") && notes === "Bootleg") return `${p} (Bootleg)`;
  if (p === "SNES" && r.releaseRegion === "Japan" && !f.accessory) return `${p} (Super Famicom)`;
  if (f.subscription) return `${p}${r.vr ? " (VR)" : ""} (${f.subscription})`;

  const store = CH_STOREFRONT[p];
  if (store) {
    const vr = r.vr && CH_VR_SPLIT.has(p) ? " (VR)" : "";
    if (r.format === "Physical" || r.format === "Both") {
      return `${p}${vr} (${r.releaseRegion || "Unknown"} Retail)`;
    }
    if (r.format === "Digital") return `${p}${vr} (${store})`;
    return `${p}${vr} (Emulation)`;
  }
  if (f.accessory) return `${p} (${f.accessory})`;            // Nintendo Power, Super Scope…
  if (p === "PlayStation Portable") {
    if (r.format === "Physical" || r.format === "Both") return `${p} (${r.releaseRegion || "Unknown"} Retail)`;
    if (r.format === "Digital") return `${p} (PSN)`;
    return `${p} (Emulation)`;
  }
  if (!r.owned && p !== "PC" && p !== "Browser") return `${p} (Emulation)`;
  return p;
}

// ExcelGame.normal_title, far enough to get the first character right.
function chFirstLetter(r) {
  const t = String(r.title).toLowerCase()
    .replace(/^(the|a|an)\s+/, "")
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^0-9a-z\s]/g, "").trim();
  const c = t[0];
  if (!c) return "?";
  return /[a-z]/.test(c) ? c.toUpperCase() : /[0-9]/.test(c) ? "#" : "?";
}

function chPlaytimeBucket(r) {
  const t = r.completed ? r.completionTime : r.estimatedTime;
  if (t == null) return "No Playtime";
  if (t < 1) return "Under 1 Hour";
  const h = Math.floor(t);
  return `${h} Hour${h !== 1 ? "s" : ""}`;
}

const chMonth = (iso) => {
  if (!iso) return null;
  const d = new Date(iso + "T00:00:00");
  return `${d.toLocaleDateString("en-US", { month: "long" })}, ${d.getFullYear()}`;
};

function chRatingBucket(r) {
  const cr = combinedRating(r);
  return cr == null ? null : `${Math.floor(cr * 10) * 10}%`;
}

function chPriceBucket(r) {
  const p = Math.trunc(r.purchasePrice || 0);
  return p > 0 ? `$${p}.00` : "Free";
}

// numpy.percentile with linear interpolation, over every game's combined rating.
let _chPct = null;
function chPercentiles() {
  if (_chPct) return _chPct;
  const vals = chRows().map(combinedRating).filter((v) => v != null).sort((a, b) => a - b);
  const q = (p) => {
    if (!vals.length) return 0;
    const idx = (vals.length - 1) * (p / 100), lo = Math.floor(idx), hi = Math.ceil(idx);
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo);
  };
  return (_chPct = { p1: q(1), p5: q(5), p10: q(10), p25: q(25), med: q(50), p75: q(75), p90: q(90), p95: q(95), p99: q(99) });
}
const chPctStr = (v) => (v * 100).toFixed(2) + "%";
function chPercentileBucket(r) {
  const cr = combinedRating(r);
  if (cr == null) return null;
  const P = chPercentiles();
  if (cr < P.p1) return `1st (<${chPctStr(P.p1)})`;
  if (cr < P.p5) return `1-5th (${chPctStr(P.p1)}-${chPctStr(P.p5)})`;
  if (cr < P.p10) return `5-10th (${chPctStr(P.p5)}-${chPctStr(P.p10)})`;
  if (cr < P.p25) return `10-25th (${chPctStr(P.p10)}-${chPctStr(P.p25)})`;
  if (cr < P.med) return `25-49th (${chPctStr(P.p25)}-${chPctStr(P.med)})`;
  if (cr < P.p75) return `50-74th (${chPctStr(P.med)}-${chPctStr(P.p75)})`;
  if (cr < P.p90) return `75-89th (${chPctStr(P.p75)}-${chPctStr(P.p90)})`;
  if (cr < P.p95) return `90-94th (${chPctStr(P.p90)}-${chPctStr(P.p95)})`;
  if (cr < P.p99) return `95-98th (${chPctStr(P.p95)}-${chPctStr(P.p99)})`;
  return `99th (>=${chPctStr(P.p99)})`;
}

// The 50 developers with the most games in the collection.
let _chTopDevs = null;
function chTopDevelopers() {
  if (_chTopDevs) return _chTopDevs;
  const counts = new Map();
  for (const r of chRows()) if (r.developer) counts.set(r.developer, (counts.get(r.developer) || 0) + 1);
  return (_chTopDevs = new Set([...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 50).map((e) => e[0])));
}

const CH_FRANCHISE_CONTENDERS = new Set([
  "Final Fantasy", "Final Fantasy Tactics", "Chocobo", "Mana", "SaGa", "Dragon Quest",
  "Megami Tensei", "Red Faction", "Castlevania", "Kirby", "Command & Conquer",
  "The Elder Scrolls", "Splinter Cell", "Alone in the Dark", "Silent Hill", "The Darkness",
  "Resident Evil", "Turok", "The Witcher", "Halo", "Infamous", "Uncharted", "Dead Island",
  "Dead Rising", "Deus Ex", "Metal Gear", "King's Field", "Armored Core", "Shadow Tower",
  "Echo Night", "Lost Kingdoms", "Otogi", "Souls", "Yakuza", "Ys", "Xeno", "Far Cry",
  "Metroid", "Call of Duty", "Ace Attorney", "Professor Layton", "Advance Wars",
  "Assassin's Creed", "Star Ocean", "Fire Emblem", "Tales", "Shining", "Phantasy Star",
  "The Legend of Heroes", "Suikoden", "Breath of Fire", "Wild Arms", "Arc the Lad", "Grandia",
  "Hyperdimension Neptunia", "Ar tonelico", "Final Fantasy Crystal Chronicles", "Atelier",
  "Kingdom Hearts", "Lunar", "Disgaea", "Etrian Odyssey", "Ogre Battle", "Picross",
  "Genkai Tokki", "Parasite Eve", "Summon Night", "Mario RPG", "Mario & Luigi", "Paper Mario",
  "Mario", "The Legend of Zelda", "Sonic the Hedgehog", "Pikmin", "Mega Man", "Mega Man X",
  "Mega Man Zero", "Mega Man Battle Network", "Mega Man Legends", "Pokémon", "Pokémon Ranger",
  "Pokémon Mystery Dungeon", "Jak and Daxter", "Ratchet & Clank", "Resistance", "Spyro",
  "Crash Bandicoot", "Sly Cooper", "Killzone", "Chibi-Robo", "Grand Theft Auto", "Doom",
]);

// ---- the challenges ------------------------------------------------------
// group:   game -> bucket key (null = not in this challenge)
// domain:  which games the challenge is about at all (default: everything)
// pool:    which games count as "still to do" (default: unplayed candidates)
// clear:   which completions can clear a bucket (default: same as domain)
// keySort: how to order buckets in the detail view (default: biggest first)
const CHALLENGES = [
  {
    id: "platform", icon: "i-dice", name: "One Per Platform",
    blurb: "Beat a game on every platform — counting the splits that actually feel different: Famicom apart from NES, XBLA apart from disc, MAME apart from the rest.",
    group: platformCompletionId,
  },
  {
    id: "genre", icon: "i-library", name: "One Per Genre",
    blurb: "Beat a game in every genre in the collection, from visual novels to twin-stick shooters.",
    group: (r) => r.genre || null,
  },
  {
    id: "year", icon: "i-calendar", name: "One Per Year",
    blurb: "Beat a game from every release year the collection covers.",
    group: (r) => r.releaseYear || null,
    keySort: (k) => -Number(k),
  },
  {
    id: "letter", icon: "i-list", name: "One Per Letter",
    blurb: "Beat a game starting with every letter of the alphabet (leading articles dropped, so The Last of Us is an L).",
    group: chFirstLetter,
    keySort: (k) => k,
  },
  {
    id: "region", icon: "i-target", name: "One Per Region",
    blurb: "Beat a game released in every region the collection reaches.",
    group: (r) => r.releaseRegion || null,
  },
  {
    id: "playtime", icon: "i-clock", name: "One Per Playtime",
    blurb: "Beat a game of every length, hour by hour — a 3-hour game, a 4-hour game, and so on up.",
    group: chPlaytimeBucket,
    keySort: (k) => (k === "No Playtime" ? 1e9 : k === "Under 1 Hour" ? -1 : parseInt(k, 10)),
  },
  {
    id: "rating", icon: "i-star", name: "One Per Rating",
    blurb: "Beat a game in every 10% band of combined rating — the great, the mediocre and the truly dire.",
    start: "2026-01-09", timesCompleted: 2,
    group: chRatingBucket,
    keySort: (k) => -parseInt(k, 10),
  },
  {
    id: "percentile", icon: "i-trend", name: "One Per Percentile",
    blurb: "Beat a game from every percentile band of the collection's rating distribution, from the bottom 1% to the top.",
    start: "2025-12-16", timesCompleted: 3,
    group: chPercentileBucket,
    keySort: (k) => -parseFloat(k),
  },
  {
    id: "length", icon: "i-sort", name: "One Per Title Length",
    blurb: "Beat a game of every title length, counted in characters with the spaces taken out.",
    group: (r) => String(r.title).replace(/ /g, "").length,
    keySort: (k) => Number(k),
  },
  {
    id: "developer", icon: "i-package", name: "One Per Top Developer",
    blurb: "Beat a game by each of the 50 developers best represented in the collection.",
    domain: (r) => chTopDevelopers().has(r.developer),
    group: (r) => r.developer || null,
  },
  {
    id: "franchise", icon: "i-trophy", name: "One Per Franchise Contender",
    blurb: "Beat a game from every franchise on the shortlist — the series worth actually playing through.",
    domain: (r) => CH_FRANCHISE_CONTENDERS.has(r.franchise),
    group: (r) => r.franchise || null,
  },
  {
    id: "added", icon: "i-plus", name: "One Per Added Date",
    blurb: "Beat a game added to the sheet in every month it's been kept — clearing the backlog a vintage at a time.",
    start: "2025-04-18", timesCompleted: 1,
    domain: (r) => !!r.dateAdded,
    group: (r) => chMonth(r.dateAdded),
    keySort: (k, rows) => rows[0].dateAdded, sortDesc: true,
  },
  {
    id: "purchased", icon: "i-package", name: "One Per Purchase Date",
    blurb: "Beat a game bought in every month I've been buying them.",
    domain: (r) => !!r.datePurchased,
    group: (r) => chMonth(r.datePurchased),
    keySort: (k, rows) => rows[0].datePurchased, sortDesc: true,
  },
  {
    id: "price", icon: "i-trend", name: "One Per Purchase Price",
    blurb: "Beat a game bought at every whole-dollar price point.",
    domain: (r) => r.purchasePrice != null && r.purchasePrice > 0,
    group: chPriceBucket,
    keySort: (k) => parseFloat(String(k).replace("$", "")) || 0,
  },
  {
    id: "limitedprint", icon: "i-package", name: "One Per Limited Print",
    blurb: "Beat a game from every limited-print label — Limited Run, iam8bit, Super Rare and the rest of the boutique pressings.",
    domain: (r) => !!chNoteFacts(r).limitedPrint,
    group: (r) => chNoteFacts(r).limitedPrint || null,
  },
  {
    id: "translation", icon: "🈳", name: "One Per Fan Translation",
    blurb: "Beat a fan-translated game on every platform — the imports that only exist in English thanks to someone's weekend.",
    domain: (r) => r.english === "Full" && !r.owned,
    clear: (r) => r.english === "Full",
    group: (r) => {
      const p = platformCompletionId(r);
      return p ? `${p} (${r.english === "Full" ? "Translated" : "Untranslated"})` : null;
    },
  },
  {
    id: "unplayable", icon: "i-alert", name: "One Per Platform (Unplayable)",
    blurb: "The stubborn half of the platform challenge: the games marked unplayable — no dump, no hardware, no way in — one per platform.",
    pool: (r) => r.playable !== "Yes" && !r.completed,
    clear: () => true,
    universe: (r) => r.playable !== "Yes",   // only platforms that HAVE unplayable games
    group: platformCompletionId,
  },
];

// ---- computation ---------------------------------------------------------

function computeChallenge(c) {
  const rows = chRows();
  const start = c.start || CH_DEFAULT_START;
  const domain = c.domain || (() => true);
  const clear = c.clear || domain;
  const pool = c.pool || isCandidate;
  const universe = c.universe || clear;

  // The buckets this challenge is even about. Without this, a completion could
  // "clear" a bucket outside the challenge — beating a game on PC would count
  // toward the Unplayable challenge even though PC has no unplayable games.
  // A challenge groups a game into one bucket, EXCEPT where the facet is itself
  // multi-valued (IGDB themes, game modes): beating one game with three themes
  // legitimately clears three buckets.
  const groupsOf = c.groupMany
    ? (r) => (c.groupMany(r) || []).filter((k) => k != null && k !== "").map(String)
    : (r) => { const k = c.group(r); return k == null || k === "" ? [] : [String(k)]; };

  const universeKeys = new Set();
  for (const r of rows) {
    if (!universe(r)) continue;
    for (const k of groupsOf(r)) universeKeys.add(k);
  }

  // Cleared: a bucket holding a game completed since the challenge began.
  const cleared = new Map();     // key -> rows, earliest completion first
  let completedSinceStart = 0;
  for (const r of rows) {
    if (!r.completed || !r.dateCompleted || r.dateCompleted <= start) continue;
    completedSinceStart++;
    if (!clear(r)) continue;
    for (const ks of groupsOf(r)) {
      if (!universeKeys.has(ks)) continue;
      if (!cleared.has(ks)) cleared.set(ks, []);
      cleared.get(ks).push(r);
    }
  }
  for (const list of cleared.values()) list.sort((a, b) => (a.dateCompleted < b.dateCompleted ? -1 : 1));

  // Remaining: every bucket in the pool that nothing has cleared yet.
  const remaining = new Map();   // key -> candidate rows, best-rated first
  for (const r of rows) {
    if (!domain(r) || !pool(r)) continue;
    for (const ks of groupsOf(r)) {
      if (cleared.has(ks)) continue;
      if (!remaining.has(ks)) remaining.set(ks, []);
      remaining.get(ks).push(r);
    }
  }
  for (const list of remaining.values()) {
    list.sort((a, b) => (combinedRating(b) ?? 0) - (combinedRating(a) ?? 0));
  }

  const total = cleared.size + remaining.size;
  return {
    c, start, cleared, remaining, total, completedSinceStart,
    pct: total ? cleared.size / total : 0,
  };
}

// Order buckets for display: the challenge's own key order, else biggest first.
function chSortBuckets(res, map) {
  const entries = [...map.entries()];
  const ks = res.c.keySort;
  entries.sort((a, b) => {
    if (ks) {
      const x = ks(a[0], a[1]), y = ks(b[0], b[1]);
      const cmp = x < y ? -1 : x > y ? 1 : 0;
      return res.c.sortDesc ? -cmp : cmp;
    }
    return b[1].length - a[1].length || String(a[0]).localeCompare(String(b[0]));
  });
  return entries;
}

// ---- rendering -----------------------------------------------------------

function chRing(pct, size = 54) {
  const r = size / 2 - 4, circ = 2 * Math.PI * r;
  return `<svg class="ch-ring" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="ch-ring-bg"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="ch-ring-fg"
      stroke-dasharray="${circ.toFixed(1)}" stroke-dashoffset="${(circ * (1 - pct)).toFixed(1)}"/>
    <text x="50%" y="50%" class="ch-ring-txt">${Math.round(pct * 100)}%</text></svg>`;
}

function chCardHtml(res) {
  const { c, cleared, remaining, total } = res;
  const done = total > 0 && remaining.size === 0;
  const times = c.timesCompleted
    ? `<span class="ch-badge">✓ cleared ${c.timesCompleted}×</span>` : "";
  return `<button class="ch-card${done ? " ch-done" : ""}" data-ch="${c.id}">
    <div class="ch-card-top">
      <span class="ch-icon">${glyph(c.icon, 20)}</span>
      ${chRing(res.pct)}
    </div>
    <h3>${escapeHtml(c.name)}</h3>
    <div class="ch-count"><b>${cleared.size}</b> of ${total} cleared</div>
    <div class="ch-bar"><span style="width:${(res.pct * 100).toFixed(1)}%"></span></div>
    <div class="ch-foot">
      <span>${remaining.size ? `${remaining.size} to go` : "Complete!"}</span>
      <span class="muted">since ${escapeHtml(fmtDate(res.start))}</span>
    </div>
    ${times}</button>`;
}

// A candidate/clearing game as a compact chip.
function chGameChip(row, note) {
  const cs = coverSrc(ENRICH[row._k], "cover_small");
  const art = cs ? `<img src="${cs}" alt="" loading="lazy">` : `<span class="ch-chip-ph">${icon("i-library", 16)}</span>`;
  const cr = combinedRating(row);
  const sub = [row.platform, row.releaseYear].filter(Boolean).map(String).map(escapeHtml).join(" · ");
  const meta = note
    ? `<span class="ch-chip-note">${escapeHtml(note)}</span>`
    : (cr != null ? `<span class="ch-chip-score ${ratingClass(cr)}">${Math.round(cr * 100)}%</span>` : "");
  return `<button class="ch-chip" data-gk="${escapeHtml(String(row._k || ""))}" data-gt="${escapeHtml(String(row.title))}" data-gp="${escapeHtml(String(row.platform || ""))}">
    ${art}<span class="ch-chip-txt"><b>${escapeHtml(String(row.title))}</b><span class="muted">${sub}</span></span>${meta}</button>`;
}

const CH_BUCKETS_SHOWN = 40;   // buckets rendered before "show all"

function chBucketList(res, map, kind) {
  const entries = chSortBuckets(res, map);
  if (!entries.length) {
    return `<div class="ch-empty">${kind === "todo" ? "Nothing left — challenge complete!" : "Nothing cleared yet."}</div>`;
  }
  const show = chState.showAll === kind ? entries : entries.slice(0, CH_BUCKETS_SHOWN);
  const html = show.map(([key, rows]) => {
    const games = kind === "todo"
      ? rows.slice(0, 5).map((r) => chGameChip(r)).join("")
      : rows.slice(0, 1).map((r) => chGameChip(r, fmtDate(r.dateCompleted))).join("");
    const extra = kind === "todo" && rows.length > 5
      ? `<span class="ch-more">+${rows.length - 5} more</span>` : "";
    return `<div class="ch-bucket${kind === "done" ? " ch-bucket-done" : ""}">
      <div class="ch-bucket-head"><h4>${escapeHtml(String(key))}</h4>
        <span class="muted">${kind === "todo" ? `${rows.length} candidate${rows.length !== 1 ? "s" : ""}` : "✓ cleared"}</span></div>
      <div class="ch-chips">${games}${extra}</div></div>`;
  }).join("");
  const rest = entries.length - show.length;
  const more = rest > 0
    ? `<button class="ch-showall" data-showall="${kind}">Show all ${entries.length}</button>` : "";
  return html + more;
}

function renderChallenges() {
  const host = $("#challenges");
  if (!DATA) return;

  if (chEditor.open) {
    host.innerHTML = chEditorHtml();
    wireEditor(host);
    host.scrollTop = 0;
    return;
  }

  const all = chAll();
  if (!chState.open) {
    const results = all.map(computeChallenge);
    const totalCleared = results.reduce((a, r) => a + r.cleared.size, 0);
    const totalBuckets = results.reduce((a, r) => a + r.total, 0);
    const finished = results.filter((r) => r.total && !r.remaining.size).length;
    host.innerHTML =
      `<div class="ch-hero">
         <h1>Challenges</h1>
         <p>One game per platform, per genre, per year, per letter… Progress is read straight from the sheet: a bucket clears the day you finish something in it.</p>
         <div class="ch-hero-stats">
           <span><b>${all.length}</b> challenges</span>
           <span><b>${totalCleared.toLocaleString()}</b> buckets cleared</span>
           <span><b>${(totalBuckets - totalCleared).toLocaleString()}</b> to go</span>
           ${finished ? `<span><b>${finished}</b> finished</span>` : ""}
         </div>
       </div>
       <div class="ch-grid">${results.map(chCardHtml).join("")}
         <button class="ch-card ch-new" id="chNew">
           <span class="ch-new-plus">＋</span>
           <b>New challenge</b>
           <span class="muted">One per anything you can filter by — themes, storefronts, developers, Steam Deck rating.</span>
         </button>
       </div>`;
    for (const el of host.querySelectorAll(".ch-card[data-ch]")) {
      el.onclick = () => { chState.open = el.dataset.ch; chState.showAll = null; renderChallenges(); nav(); };
    }
    $("#chNew").onclick = () => chOpenEditor(null);
    return;
  }

  const c = all.find((x) => x.id === chState.open) || all[0];
  const res = computeChallenge(c);
  const times = c.timesCompleted
    ? `<span class="ch-badge">✓ cleared ${c.timesCompleted}× already</span>` : "";
  host.innerHTML =
    `<div class="ch-detail">
       <button class="ch-back" id="chBack">← All challenges</button>
       ${c.custom ? `<button class="btn ghost ch-edit" id="chEdit">✎ Edit challenge</button>` : ""}
       <div class="ch-detail-head">
         <span class="ch-icon big">${glyph(c.icon, 30)}</span>
         <div>
           <h1>${escapeHtml(c.name)}</h1>
           <p>${escapeHtml(c.blurb)}</p>
           ${times}
         </div>
         ${chRing(res.pct, 92)}
       </div>
       <div class="ch-bar big"><span style="width:${(res.pct * 100).toFixed(1)}%"></span></div>
       <div class="ch-stats">
         <div><b>${res.cleared.size}</b><span>cleared</span></div>
         <div><b>${res.remaining.size}</b><span>remaining</span></div>
         <div><b>${res.total}</b><span>total</span></div>
         <div><b>${escapeHtml(fmtDate(res.start))}</b><span>started</span></div>
         <div><b>${res.completedSinceStart.toLocaleString()}</b><span>games beaten since</span></div>
       </div>
       <h2 class="ch-sec">Still to do <span class="muted">${res.remaining.size}</span></h2>
       <p class="ch-hint">Top-rated candidates for each, five shown. Tap any game for details.</p>
       <div class="ch-buckets">${chBucketList(res, res.remaining, "todo")}</div>
       <h2 class="ch-sec">Cleared <span class="muted">${res.cleared.size}</span></h2>
       <div class="ch-buckets">${chBucketList(res, res.cleared, "done")}</div>
     </div>`;

  $("#chBack").onclick = () => { chState.open = null; chState.showAll = null; renderChallenges(); nav(); };
  const edit = $("#chEdit");
  if (edit) edit.onclick = () => chOpenEditor(c.custom);
  for (const el of host.querySelectorAll(".ch-showall")) {
    el.onclick = () => { chState.showAll = el.dataset.showall; renderChallenges(); };
  }
  for (const el of host.querySelectorAll(".ch-chip")) {
    el.onclick = () => {
      const row = chRows().find((r) =>
        String(r._k || "") === el.dataset.gk &&
        String(r.title) === el.dataset.gt &&
        String(r.platform || "") === el.dataset.gp);
      if (row) openDrawer(row, "games");
    };
  }
  host.scrollIntoView({ block: "start" });
}

/* ===========================================================================
   CUSTOM CHALLENGES

   The built-ins are hand-written because each encodes a judgement — which
   platform splits "count" as different, which franchises are contenders. But the
   machinery underneath is general: a challenge is a way of grouping games into
   buckets, plus a domain saying which games are in play. Both of those are things
   the facet system already knows how to compute for any column, including the
   enrichment-derived ones (IGDB theme, game mode, Steam Deck status).

   So a custom challenge is: group by a facet, optionally filter by other facets,
   pick a start date. It then runs through exactly the same computeChallenge as
   the built-ins — same clearing rules, same candidate pool, same progress.

   Stored in localStorage: gamedex has no accounts, and this is a personal goal
   rather than shared data. Same place the saved views live.
   =========================================================================== */

const CH_CUSTOM_KEY = "gamedex.challenges";

const chLoadCustom = () => {
  try { return JSON.parse(localStorage.getItem(CH_CUSTOM_KEY) || "[]"); }
  catch (_) { return []; }
};
// Write-through to the server (see extras.js); localStorage stays as the offline
// mirror, so a challenge built on the desktop shows up on the phone.
const chStoreCustom = (list) => prefsSave("challenges", list.slice(0, 40));

// Facets you can group a challenge by. Straight from the games sheet's own facet
// columns, so anything filterable is also groupable — no separate list to keep in
// sync as facets are added.
function chGroupables() {
  const sheetCols = ((DATA.sheets.games || {}).columns || []).filter((c) => c.facet);
  // Ask for the GAMES tab's facets explicitly: we're sitting on the Challenges
  // tab, and these builders default to whatever tab is active.
  const igdb = typeof igdbFacetCols === "function" ? igdbFacetCols("games") : [];
  const extra = typeof extraFacetCols === "function" ? extraFacetCols("games") : [];
  return [...sheetCols, ...igdb, ...extra];
}
const chColByKey = (key) => chGroupables().find((c) => c.key === key);

// The values a row falls into for a facet — reuses the grid's own accessor, so a
// multi-valued facet (themes) yields several buckets and a plain one yields one.
function chFacetVals(row, col) {
  if (!col) return [];
  return (typeof rowFacetItems === "function" ? rowFacetItems(row, col) : []).map((i) => i.key);
}

// A stored definition -> a challenge object computeChallenge understands.
function chFromCustom(def) {
  const groupCol = chColByKey(def.groupBy);
  const filters = (def.filters || []).map((f) => ({ col: chColByKey(f.key), values: new Set(f.values) }))
    .filter((f) => f.col && f.values.size);
  return {
    id: def.id,
    icon: def.icon || "i-target",
    name: def.name || "Custom challenge",
    blurb: chCustomBlurb(def),
    start: def.start || CH_DEFAULT_START,
    custom: def,
    // Every filter must match (AND across facets, OR within one).
    domain: (r) => filters.every((f) => chFacetVals(r, f.col).some((v) => f.values.has(v))),
    groupMany: (r) => chFacetVals(r, groupCol),
  };
}

function chCustomBlurb(def) {
  const g = chColByKey(def.groupBy);
  const gl = g ? g.label : def.groupBy;
  const fs = (def.filters || []).filter((f) => (f.values || []).length).map((f) => {
    const c = chColByKey(f.key);
    const lbl = (k) => (c && c.type === "bool") ? (k === "true" ? "Yes" : "No") : k;
    const vs = f.values.map(lbl);
    return `${c ? c.label : f.key} is ${vs.slice(0, 3).join(" or ")}${vs.length > 3 ? ` (+${vs.length - 3})` : ""}`;
  });
  return `Beat one game per ${gl}${fs.length ? `, limited to games where ${fs.join(" and ")}` : ""}.`;
}

// Built-ins first, then yours.
const chAll = () => [...CHALLENGES, ...chLoadCustom().map(chFromCustom)];

// ---- the builder ---------------------------------------------------------

const chEditor = { open: false, def: null };

const chBlankDef = () => ({
  id: "custom-" + Math.random().toString(36).slice(2, 9),
  name: "", icon: "i-target", groupBy: "platform", filters: [],
  start: new Date().toISOString().slice(0, 10),
});

function chEditorHtml() {
  const d = chEditor.def;
  const cols = chGroupables();
  const opt = (c, sel) => `<option value="${escapeHtml(c.key)}"${c.key === sel ? " selected" : ""}>${escapeHtml(c.label)}</option>`;
  const filterRows = (d.filters || []).map((f, i) => {
    const col = chColByKey(f.key);
    const vals = col ? chFacetValues(col) : [];
    return `<div class="chb-filter" data-fi="${i}">
      <select class="chb-fkey">${cols.map((c) => opt(c, f.key)).join("")}</select>
      <select class="chb-fval" multiple size="4">${vals.map((v) =>
        `<option value="${escapeHtml(v.key)}"${(f.values || []).includes(v.key) ? " selected" : ""}>${escapeHtml(v.label)} (${v.n})</option>`).join("")}</select>
      <button class="chb-del" data-fi="${i}" title="Remove this filter">✕</button>
    </div>`;
  }).join("");

  return `<div class="chb">
    <div class="chb-head">
      <h2>${d._editing ? "Edit" : "New"} challenge</h2>
      <button class="chb-close" id="chbClose">✕</button>
    </div>
    <label class="chb-row"><span>Name</span>
      <input id="chbName" type="text" value="${escapeHtml(d.name)}" placeholder="One per Steam Deck rating…" maxlength="60">
    </label>
    <label class="chb-row"><span>Icon</span>
      <input id="chbIcon" type="text" value="${escapeHtml(d.icon)}" maxlength="4" style="width:64px">
    </label>
    <label class="chb-row"><span>One per…</span>
      <select id="chbGroup">${cols.map((c) => opt(c, d.groupBy)).join("")}</select>
    </label>
    <label class="chb-row"><span>Counting from</span>
      <input id="chbStart" type="date" value="${escapeHtml(d.start)}">
      <em>Completions before this date don't count — the same rule the built-ins use.</em>
    </label>
    <div class="chb-row chb-filters">
      <span>Only these games</span>
      <div>
        ${filterRows || `<p class="muted">No filter — every game is in play.</p>`}
        <button class="btn ghost" id="chbAddFilter">+ Add a filter</button>
      </div>
    </div>
    <div class="chb-preview" id="chbPreview"></div>
    <div class="chb-actions">
      ${d._editing ? `<button class="btn danger" id="chbDelete">Delete</button>` : ""}
      <span class="spacer"></span>
      <button class="btn ghost" id="chbCancel">Cancel</button>
      <button class="btn launch" id="chbSave">Save challenge</button>
    </div>
  </div>`;
}

// Distinct values for a facet, most common first — the same values the sidebar
// offers, so a filter can't be built out of something that doesn't exist.
function chFacetValues(col) {
  const counts = new Map();
  for (const r of chRows()) {
    for (const it of (rowFacetItems(r, col) || [])) {
      const k = it.key;
      if (!counts.has(k)) counts.set(k, { n: 0, label: facetLabel(col, it.raw) });
      counts.get(k).n++;
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1].n - a[1].n)
    .slice(0, 200)
    .map(([key, v]) => ({ key, label: v.label, n: v.n }));
}

// Live preview: how many buckets would this challenge actually have?
function chPreview() {
  const host = $("#chbPreview");
  if (!host) return;
  try {
    const res = computeChallenge(chFromCustom(chEditor.def));
    host.innerHTML = res.total
      ? `<b>${res.total}</b> buckets · <b>${res.cleared.size}</b> already cleared by past completions · <b>${res.remaining.size}</b> to go`
      : `<span class="muted">No buckets — that combination of facet and filters matches nothing.</span>`;
  } catch (e) {
    host.innerHTML = `<span class="muted">Can't evaluate that yet.</span>`;
  }
}

function wireEditor(host) {
  const d = chEditor.def;
  const close = () => { chEditor.open = false; chEditor.def = null; renderChallenges(); };
  $("#chbClose").onclick = close;
  $("#chbCancel").onclick = close;
  $("#chbName").oninput = (e) => { d.name = e.target.value; };
  $("#chbIcon").oninput = (e) => { d.icon = e.target.value; };
  $("#chbGroup").onchange = (e) => { d.groupBy = e.target.value; chPreview(); };
  $("#chbStart").onchange = (e) => { d.start = e.target.value; chPreview(); };

  $("#chbAddFilter").onclick = () => {
    d.filters = d.filters || [];
    d.filters.push({ key: chGroupables()[0].key, values: [] });
    renderChallenges();
  };
  host.querySelectorAll(".chb-del").forEach((el) => {
    el.onclick = () => { d.filters.splice(+el.dataset.fi, 1); renderChallenges(); };
  });
  host.querySelectorAll(".chb-fkey").forEach((el) => {
    el.onchange = () => {
      const i = +el.closest(".chb-filter").dataset.fi;
      d.filters[i] = { key: el.value, values: [] };   // values belong to the old facet
      renderChallenges();
    };
  });
  host.querySelectorAll(".chb-fval").forEach((el) => {
    el.onchange = () => {
      const i = +el.closest(".chb-filter").dataset.fi;
      d.filters[i].values = [...el.selectedOptions].map((o) => o.value);
      chPreview();
    };
  });

  $("#chbSave").onclick = () => {
    if (!d.name.trim()) { $("#chbName").focus(); return; }
    const list = chLoadCustom();
    const i = list.findIndex((x) => x.id === d.id);
    const clean = { id: d.id, name: d.name.trim(), icon: d.icon || "i-target",
                    groupBy: d.groupBy, filters: d.filters || [], start: d.start };
    if (i >= 0) list[i] = clean; else list.push(clean);
    chStoreCustom(list);
    chEditor.open = false; chEditor.def = null;
    showToast(`Challenge "${clean.name}" saved`);
    renderChallenges();
  };
  const del = $("#chbDelete");
  if (del) del.onclick = () => {
    chStoreCustom(chLoadCustom().filter((x) => x.id !== d.id));
    chEditor.open = false; chEditor.def = null;
    showToast("Challenge deleted");
    renderChallenges();
  };
  chPreview();
}

function chOpenEditor(def) {
  chEditor.open = true;
  chEditor.def = def ? { ...def, _editing: true } : chBlankDef();
  renderChallenges();
}
