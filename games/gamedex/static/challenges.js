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
  const mine = r.rating != null ? r.rating : (r.priority != null ? Number(r.priority) / 5 : null);
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
  if (!((Number(r.priority) || 0) > 1)) return false;
  if (r.playable !== "Yes") return false;
  if (r.english === "None" && CH_TEXT_GENRES.has(r.genre)) return false;
  return !!r.releaseDate && r.releaseDate <= chToday();
}

// ---- bucket keys ---------------------------------------------------------

// get_platform_completion_id: platform, split by the distinctions that make a
// playthrough feel different — Famicom vs NES, XBLA vs disc, MAME vs not.
// (GamePicker also splits on subscription service, required accessory and
// digital storefront; the sheet doesn't carry those columns, so we don't.)
const CH_STOREFRONT = {
  "Xbox 360": "XBLA", "PlayStation 3": "PSN", "PlayStation 4": "PSN", "PlayStation 5": "PSN",
  "PlayStation Vita": "PSN", "PlayStation Portable": "PSN", "Nintendo 3DS": "eShop",
  "New Nintendo 3DS": "eShop", "Nintendo Wii U": "eShop", "Nintendo Switch": "eShop",
  "Nintendo Switch 2": "eShop", "Xbox": "Digital", "Xbox One": "Digital",
  "Xbox Series X|S": "Digital",
};
const CH_VR_SPLIT = new Set(["PlayStation 4", "PlayStation 5"]);

function platformCompletionId(r) {
  const p = r.platform;
  if (!p) return null;
  if (r.dlc) return `${p} (DLC)`;
  if (p === "Arcade") {
    if (r.notes) return `${p} (${r.notes})`;
    return `${p} (${r.mameRomset ? "MAME" : "Non-MAME"})`;
  }
  if (p === "NES" && r.releaseRegion === "Japan") return `${p} (Famicom)`;
  if ((p === "NES" || p === "Game Boy Color") && r.notes === "Bootleg") return `${p} (Bootleg)`;
  if (p === "SNES" && r.releaseRegion === "Japan") return `${p} (Super Famicom)`;

  const store = CH_STOREFRONT[p];
  if (store) {
    const vr = r.vr && CH_VR_SPLIT.has(p) ? " (VR)" : "";
    if (r.format === "Physical" || r.format === "Both") {
      return `${p}${vr} (${r.releaseRegion || "Unknown"} Retail)`;
    }
    if (r.format === "Digital") return `${p}${vr} (${store})`;
    return `${p}${vr} (Emulation)`;
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
    id: "platform", icon: "🕹️", name: "One Per Platform",
    blurb: "Beat a game on every platform — counting the splits that actually feel different: Famicom apart from NES, XBLA apart from disc, MAME apart from the rest.",
    group: platformCompletionId,
  },
  {
    id: "genre", icon: "🎭", name: "One Per Genre",
    blurb: "Beat a game in every genre in the collection, from visual novels to twin-stick shooters.",
    group: (r) => r.genre || null,
  },
  {
    id: "year", icon: "📅", name: "One Per Year",
    blurb: "Beat a game from every release year the collection covers.",
    group: (r) => r.releaseYear || null,
    keySort: (k) => -Number(k),
  },
  {
    id: "letter", icon: "🔤", name: "One Per Letter",
    blurb: "Beat a game starting with every letter of the alphabet (leading articles dropped, so The Last of Us is an L).",
    group: chFirstLetter,
    keySort: (k) => k,
  },
  {
    id: "region", icon: "🌍", name: "One Per Region",
    blurb: "Beat a game released in every region the collection reaches.",
    group: (r) => r.releaseRegion || null,
  },
  {
    id: "playtime", icon: "⏱️", name: "One Per Playtime",
    blurb: "Beat a game of every length, hour by hour — a 3-hour game, a 4-hour game, and so on up.",
    group: chPlaytimeBucket,
    keySort: (k) => (k === "No Playtime" ? 1e9 : k === "Under 1 Hour" ? -1 : parseInt(k, 10)),
  },
  {
    id: "rating", icon: "⭐", name: "One Per Rating",
    blurb: "Beat a game in every 10% band of combined rating — the great, the mediocre and the truly dire.",
    start: "2026-01-09", timesCompleted: 2,
    group: chRatingBucket,
    keySort: (k) => -parseInt(k, 10),
  },
  {
    id: "percentile", icon: "📈", name: "One Per Percentile",
    blurb: "Beat a game from every percentile band of the collection's rating distribution, from the bottom 1% to the top.",
    start: "2025-12-16", timesCompleted: 3,
    group: chPercentileBucket,
    keySort: (k) => -parseFloat(k),
  },
  {
    id: "length", icon: "📏", name: "One Per Title Length",
    blurb: "Beat a game of every title length, counted in characters with the spaces taken out.",
    group: (r) => String(r.title).replace(/ /g, "").length,
    keySort: (k) => Number(k),
  },
  {
    id: "developer", icon: "🏢", name: "One Per Top Developer",
    blurb: "Beat a game by each of the 50 developers best represented in the collection.",
    domain: (r) => chTopDevelopers().has(r.developer),
    group: (r) => r.developer || null,
  },
  {
    id: "franchise", icon: "⚔️", name: "One Per Franchise Contender",
    blurb: "Beat a game from every franchise on the shortlist — the series worth actually playing through.",
    domain: (r) => CH_FRANCHISE_CONTENDERS.has(r.franchise),
    group: (r) => r.franchise || null,
  },
  {
    id: "added", icon: "➕", name: "One Per Added Date",
    blurb: "Beat a game added to the sheet in every month it's been kept — clearing the backlog a vintage at a time.",
    start: "2025-04-18", timesCompleted: 1,
    domain: (r) => !!r.dateAdded,
    group: (r) => chMonth(r.dateAdded),
    keySort: (k, rows) => rows[0].dateAdded, sortDesc: true,
  },
  {
    id: "purchased", icon: "🛒", name: "One Per Purchase Date",
    blurb: "Beat a game bought in every month I've been buying them.",
    domain: (r) => !!r.datePurchased,
    group: (r) => chMonth(r.datePurchased),
    keySort: (k, rows) => rows[0].datePurchased, sortDesc: true,
  },
  {
    id: "price", icon: "💵", name: "One Per Purchase Price",
    blurb: "Beat a game bought at every whole-dollar price point.",
    domain: (r) => r.purchasePrice != null && r.purchasePrice > 0,
    group: chPriceBucket,
    keySort: (k) => parseFloat(String(k).replace("$", "")) || 0,
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
    id: "unplayable", icon: "🚫", name: "One Per Platform (Unplayable)",
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
  const universeKeys = new Set();
  for (const r of rows) {
    if (!universe(r)) continue;
    const k = c.group(r);
    if (k != null && k !== "") universeKeys.add(String(k));
  }

  // Cleared: a bucket holding a game completed since the challenge began.
  const cleared = new Map();     // key -> rows, earliest completion first
  let completedSinceStart = 0;
  for (const r of rows) {
    if (!r.completed || !r.dateCompleted || r.dateCompleted <= start) continue;
    completedSinceStart++;
    if (!clear(r)) continue;
    const k = c.group(r);
    if (k == null || k === "") continue;
    const ks = String(k);
    if (!universeKeys.has(ks)) continue;
    if (!cleared.has(ks)) cleared.set(ks, []);
    cleared.get(ks).push(r);
  }
  for (const list of cleared.values()) list.sort((a, b) => (a.dateCompleted < b.dateCompleted ? -1 : 1));

  // Remaining: every bucket in the pool that nothing has cleared yet.
  const remaining = new Map();   // key -> candidate rows, best-rated first
  for (const r of rows) {
    if (!domain(r) || !pool(r)) continue;
    const k = c.group(r);
    if (k == null || k === "") continue;
    const ks = String(k);
    if (cleared.has(ks)) continue;
    if (!remaining.has(ks)) remaining.set(ks, []);
    remaining.get(ks).push(r);
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
      <span class="ch-icon">${c.icon}</span>
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
  const art = cs ? `<img src="${cs}" alt="" loading="lazy">` : `<span class="ch-chip-ph">🎮</span>`;
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

  if (!chState.open) {
    const results = CHALLENGES.map(computeChallenge);
    const totalCleared = results.reduce((a, r) => a + r.cleared.size, 0);
    const totalBuckets = results.reduce((a, r) => a + r.total, 0);
    const finished = results.filter((r) => r.total && !r.remaining.size).length;
    host.innerHTML =
      `<div class="ch-hero">
         <h1>Challenges</h1>
         <p>One game per platform, per genre, per year, per letter… Progress is read straight from the sheet: a bucket clears the day you finish something in it.</p>
         <div class="ch-hero-stats">
           <span><b>${CHALLENGES.length}</b> challenges</span>
           <span><b>${totalCleared.toLocaleString()}</b> buckets cleared</span>
           <span><b>${(totalBuckets - totalCleared).toLocaleString()}</b> to go</span>
           ${finished ? `<span><b>${finished}</b> finished</span>` : ""}
         </div>
       </div>
       <div class="ch-grid">${results.map(chCardHtml).join("")}</div>`;
    for (const el of host.querySelectorAll(".ch-card")) {
      el.onclick = () => { chState.open = el.dataset.ch; chState.showAll = null; renderChallenges(); nav(); };
    }
    return;
  }

  const c = CHALLENGES.find((x) => x.id === chState.open) || CHALLENGES[0];
  const res = computeChallenge(c);
  const times = c.timesCompleted
    ? `<span class="ch-badge">✓ cleared ${c.timesCompleted}× already</span>` : "";
  host.innerHTML =
    `<div class="ch-detail">
       <button class="ch-back" id="chBack">← All challenges</button>
       <div class="ch-detail-head">
         <span class="ch-icon big">${c.icon}</span>
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
