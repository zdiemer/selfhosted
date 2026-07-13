"use strict";

// ---- config -------------------------------------------------------------
let PAGE_SIZE = 50;
// How each tab presents its rows. This is PER TAB: one shared global meant the
// Completed tab's timeline followed you onto other tabs and rendered there.
//   view    — "table" | "grid" | "timeline" (Completed only)
//   combine — fold rows that are the same IGDB game into one entry. Orthogonal
//             to the view: a list can be combined just as a grid can.
const VIEW_DEFAULT = { games: "grid", completed: "timeline", onOrder: "grid" };
const COMBINE_DEFAULT = { games: true, completed: false, onOrder: false };
const FACET_CAP = 12;              // values shown before "show more"
const FACET_FILTER_THRESHOLD = 12; // show a per-facet search box past this many values

// ---- state --------------------------------------------------------------
let DATA = null;            // {meta, sheets}
let activeTab = "home";
const TABS = ["games", "completed", "onOrder"];
// Per-tab UI state, isolated so switching tabs preserves filters.
const tabState = {};
// Filters/search/sort/page — wiped when you navigate to a tab afresh.
const freshState = () => ({ search: "", facets: {}, expanded: {}, sort: null, page: 1 });
// View/combine are display PREFERENCES, not filters: they survive a tab switch.
for (const t of TABS) {
  tabState[t] = { ...freshState(), view: VIEW_DEFAULT[t], combine: COMBINE_DEFAULT[t] };
}
const viewOf = () => tabState[activeTab].view;
const combineOn = () => tabState[activeTab].combine;

const $ = (sel) => document.querySelector(sel);

/* Icons. `icon("i-play")` -> inline SVG that inherits currentColor.
   `glyph(v)` renders an icon id if it looks like one, and otherwise passes the
   value straight through — because a custom challenge's icon is an emoji YOU
   chose, and that's data, not chrome. */
const icon = (id, size = 16) =>
  `<svg class="ico" width="${size}" height="${size}" aria-hidden="true"><use href="#${id}"/></svg>`;
const glyph = (v, size = 16) =>
  (typeof v === "string" && v.startsWith("i-")) ? icon(v, size) : `<span class="emo">${v || ""}</span>`;


// ---- formatting ---------------------------------------------------------
function fmtHours(h) {
  const total = Math.round(h * 60);
  const hrs = Math.floor(total / 60);
  const mins = total % 60;
  if (hrs && mins) return `${hrs}h ${mins}m`;
  if (hrs) return `${hrs}h`;
  return `${mins}m`;
}
function fmtDate(iso) {
  if (typeof iso !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function ratingClass(v) {
  return v >= 0.8 ? "rating-good" : v >= 0.6 ? "rating-mid" : "rating-bad";
}

// Returns an HTML string for a cell value given its column type.
function fmtCell(value, type) {
  if (value === undefined || value === null || value === "") return `<span class="muted">—</span>`;
  switch (type) {
    case "rating":
      return `<span class="${ratingClass(value)}">${Math.round(value * 100)}%</span>`;
    case "bool":
      return value ? `<span class="yes">Yes</span>` : `<span class="no">No</span>`;
    case "hours":
      return fmtHours(value);
    case "date":
      return escapeHtml(fmtDate(value));
    case "number":
      return typeof value === "number" ? escapeHtml(value.toLocaleString()) : escapeHtml(String(value));
    case "money":
      return typeof value === "number"
        ? "$" + escapeHtml(value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
        : escapeHtml(String(value));
    default:
      return escapeHtml(String(value));
  }
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---- IGDB enrichment (lazy, per visible page) ---------------------------
const IMG = (id, size) => (id ? `https://images.igdb.com/igdb/image/upload/t_${size}/${id}.jpg` : "");
// Cover URL: fallback sources give a full coverUrl; IGDB gives an image id.
// Cover: IGDB image id, else a fallback source's full URL, else the art the
// gated sources bring — an arcade cabinet scan or a VN cover beats a blank box.
const coverSrc = (e, size) => (
  !e ? "" :
  e.coverUrl ? e.coverUrl :
  e.cover ? IMG(e.cover, size) :
  e.vnCover || e.adbCover || e.thumbyCover || "");
// Thumby art is a 64x64 icon — scale it up with hard edges, not a blur.
const coverIsPixelArt = (e, src) => !!(e && e.thumbyCover && src === e.thumbyCover);
let ENRICH_ENABLED = false;
let ENRICH_COMPLETE = false;       // all sources backfilled → stop shimmering covers
let ENRICH_SOURCES = [];           // enabled secondary sources (hltb, metacritic, gameye)
const ENRICH = {};                 // matchKey -> light enrichment
const DETAIL = {};                 // matchKey -> full IGDB detail (drawer cache)
const HLTBC = {};                  // matchKey -> HLTB playtimes (drawer cache)
const MCC = {};                    // matchKey -> Metacritic score (drawer cache)
const GEC = {};                    // matchKey -> GameEye prices (drawer cache)
const ADBC = {};                   // matchKey -> Arcade Database record
const VNC = {};                    // matchKey -> VNDB record
const VGC = {};                    // matchKey -> VGChartz record
const THC = {};                    // matchKey -> Thumby record
const SXC = {};                    // matchKey -> Steam extras (Deck/Proton/SteamSpy)
const SRC = {};                    // matchKey -> speedrun record
const GDC = {};                    // matchKey -> StrategyWiki guide
const COOPC = {};                  // matchKey -> Co-Optimus co-op details
const ENRICH_REQUESTED = new Set();
let enrichTimer = null;
let drawerRow = null;              // row currently shown in the drawer (for sheet fallback)

// Games we looked up and found NOTHING for. They are absent from ENRICH exactly
// like a game we haven't got to yet, which is why they used to shimmer forever:
// "still looking" and "looked, found nothing" were indistinguishable.
let NO_MATCH = new Set();

// A cover is "pending" only while enrichment is still LOOKING for this row. Once
// it has resolved — with a cover or without one — it is not pending any more.
const coverPending = (row) =>
  ENRICH_ENABLED && !ENRICH_COMPLETE && !(row._k in ENRICH) && !NO_MATCH.has(row._k);
function coverCell(row) {
  const src = coverSrc(ENRICH[row._k], "cover_small");
  if (src) return `<img class="cover-thumb" loading="lazy" src="${src}" alt="">`;
  return `<span class="cover-ph${coverPending(row) ? " skel" : ""}"></span>`;
}

// Queue enrichment for any on-screen rows we haven't asked about yet.
function maybeEnrich(rows) {
  if (!ENRICH_ENABLED) return;
  const need = [...new Set(rows.map((r) => r._k).filter(Boolean))]
    .filter((k) => !(k in ENRICH) && !ENRICH_REQUESTED.has(k));
  if (need.length) postEnrich(need);
}

async function postEnrich(keys) {
  keys.forEach((k) => ENRICH_REQUESTED.add(k));
  try {
    const res = await fetch("api/enrichment", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }),
    });
    if (!res.ok) return;
    const j = await res.json();
    if (j.enabled === false) { ENRICH_ENABLED = false; return; }
    let changed = false;
    for (const [k, v] of Object.entries(j.items || {})) { ENRICH[k] = v; changed = true; }
    updateEnrichStatus(j.stats);
    if (changed) { _enrichEpoch++; resetSearchCache(); patchEnrichedCells(); }   // in-place: no flicker
    if (j.pending && j.pending.length) {                    // still resolving — poll
      clearTimeout(enrichTimer);
      enrichTimer = setTimeout(() => postEnrich(j.pending), 2500);
    }
  } catch (_) { /* transient */ }
}

function updateEnrichStatus(stats) {
  const el = $("#enrichstatus");
  if (!el || !stats) return;
  const src = stats.sources || {};
  const parts = [`IGDB ${(stats.matched || 0).toLocaleString()}`];
  if (src.hltb) parts.push(`HLTB ${(src.hltb.matched || 0).toLocaleString()}`);
  if (src.metacritic) parts.push(`MC ${(src.metacritic.matched || 0).toLocaleString()}`);
  let queued = stats.queued || 0;
  for (const s of Object.values(src)) queued += s.queued || 0;
  el.textContent = parts.join(" · ") + (queued ? ` · ${queued.toLocaleString()} queued` : "");
  el.hidden = false;

  // Enrichment progress bar (all sources combined).
  ENRICH_COMPLETE = !!stats.complete;
  const wrap = $("#progress"), bar = $("#progressBar");
  if (wrap && bar && stats.total) {
    const srcs = Object.values(src);
    const done = (stats.resolved || 0) + srcs.reduce((a, s) => a + (s.resolved || 0), 0);
    const total = stats.total * (1 + srcs.length);
    bar.style.width = Math.min(100, Math.round((100 * done) / total)) + "%";
    wrap.hidden = ENRICH_COMPLETE;
  }
}

// Shimmering placeholder cards while the spreadsheet loads.
function showSkeletons(n = 30) {
  $("#gridwrap").hidden = false;
  $("#grid").innerHTML = Array.from({ length: n }, () =>
    `<div class="card"><div class="card-cover ph skel"></div><div class="card-body">
      <div class="skel skel-line"></div><div class="skel skel-line short"></div></div></div>`).join("");
}

const chips = (arr, fk) => (arr && arr.length
  ? `<div class="chips">${arr.map((x) => fk
      ? `<span class="chip facet-link" data-fk="${fk}" data-fv="${escapeHtml(String(x))}">${escapeHtml(String(x))}</span>`
      : `<span class="chip">${escapeHtml(String(x))}</span>`).join("")}</div>` : "");

// The carousel's contents: trailers first (they autoplay, muted, when their
// slide is showing), then screenshots, then key art.
function mediaOf(d) {
  return [
    ...(d.videos || []).slice(0, 6).map((v) => ({ kind: "video", id: v.id, name: v.name || "Trailer" })),
    ...(d.screenshots || []).map((id) => ({ kind: "image", id })),
    ...(d.artworks || []).map((id) => ({ kind: "image", id, art: true })),
  ];
}

function detailHtml(d) {
  if (!d) return "";
  const cs = coverSrc(d, "cover_big");
  const cover = cs ? `<img class="cover-big" src="${cs}" alt="">` : "";
  const badge = d.manual ? `<span class="chip manual">★ Manually mapped</span>` : "";
  const rating = d.rating != null
    ? `<div class="igdb-rating ${ratingClass(d.rating)}">${Math.round(d.rating * 100)}<small>/100 IGDB</small>${d.ratingCount ? ` · ${d.ratingCount} ratings` : ""}</div>` : "";
  const linkList = (arr, fk) => arr.map((x) =>
    `<a class="facet-link" data-fk="${fk}" data-fv="${escapeHtml(String(x))}">${escapeHtml(String(x))}</a>`).join(", ");
  const meta = [];
  const uniq = (a) => [...new Set(a)];
  if (d.developers && d.developers.length) meta.push(`<div class="detail-row"><div class="k">Developer</div><div class="v">${linkList(uniq(d.developers.map(canonDev)), "developer")}</div></div>`);
  if (d.publishers && d.publishers.length) meta.push(`<div class="detail-row"><div class="k">Publisher</div><div class="v">${linkList(uniq(d.publishers.map(canonPub)), "publisher")}</div></div>`);
  if (d.franchises && d.franchises.length) meta.push(`<div class="detail-row"><div class="k">Franchise</div><div class="v">${linkList(uniq(d.franchises.map(canonFran)), "franchise")}</div></div>`);
  const nShots = mediaOf(d).length;
  const shots = nShots
    ? `<div class="shots"><div class="shot-view"></div>` +
      (nShots > 1 ? `<button class="shot-nav prev" aria-label="Previous">‹</button><button class="shot-nav next" aria-label="Next">›</button>` : "") +
      `<div class="shot-count"></div><div class="shot-cap"></div></div>` : "";
  // Only the ones you actually own. IGDB's similar list is mostly games you've
  // never heard of and don't have; as an external link it was a store aisle.
  // Filtered to the collection it becomes a real "you have this, and it's like
  // this" — and every entry opens in-app instead of navigating away.
  const similar = (typeof similarInCollection === "function" ? similarInCollection(d) : [])
    .slice(0, 12);
  const simHtml = similar.length
    ? `<div class="detail-row notes"><div class="k">Similar games you own <span class="muted">${similar.length}</span></div>
        <div class="similar">${similar.map((s) => {
          const cover = s.cover ? IMG(s.cover, "cover_small") : coverSrc(ENRICH[s.row._k], "cover_small");
          const mark = s.row.completed ? `<i class="sim-done" title="Beaten">✓</i>`
            : s.row.owned ? `<i class="sim-owned" title="Owned">●</i>` : "";
          return `<button class="sim" data-simk="${escapeHtml(String(s.row._k || ""))}" title="${escapeHtml(s.name)}">
            ${cover ? `<img loading="lazy" src="${escapeHtml(cover)}" alt="">` : `<span class="sim-ph">${icon("i-library", 18)}</span>`}
            ${mark}
            <span>${escapeHtml(s.name)}</span>
          </button>`;
        }).join("")}</div></div>`
    : "";

  const text = d.summary || d.storyline;
  // Genre / theme / mode, under the summary. They're a way INTO the collection
  // (every chip is a filter), so they belong next to the prose that made you
  // curious, not stacked above the cover.
  // Genre chips filter the UNIFIED genre facet (canonicalised so an IGDB "Platform"
  // chip filters "Platformer"); themes/modes stay IGDB-only facets.
  const genreChips = [...new Set((d.genres || []).map((g) => String(canonGenre(g))))];
  const tags = chips(genreChips, "genre") + chips(d.themes, "__igdb_theme")
    + chips(d.gameModes, "__igdb_mode");
  return (badge ? `<div class="badges">${badge}</div>` : "") +
    (text ? `<div class="detail-row notes"><div class="k">Summary (IGDB)</div><div class="v">${escapeHtml(text)}</div></div>` : "") +
    (tags ? `<div class="detail-row notes tag-row"><div class="k">Tags</div><div class="v">${tags}</div></div>` : "") +
    meta.join("") + shots + simHtml +
    igdbAttr(d);
}

// ---- cinematic hero ------------------------------------------------------
// A screenshot, blurred and dimmed, sits behind the cover and title; the numbers
// that matter become a stat strip. Built from the light enrichment immediately,
// then upgraded in place when the full detail lands.
function heroStatsHtml(row) {
  const e = ENRICH[row._k] || {};
  const pct = (v) => `${Math.round(v * 100)}`;
  const cells = [];
  const mine = row.rating != null ? row.rating : null;
  if (mine != null) cells.push([pct(mine), "My rating", ratingClass(mine)]);
  const mc = metacriticOf(row);
  if (mc != null) cells.push([pct(mc), "Critics", ratingClass(mc)]);
  const ur = userRatingOf(row);
  if (ur != null) cells.push([pct(ur), "Players", ratingClass(ur)]);
  const pt = playtimeOf(row);
  if (pt != null) cells.push([fmtHours(pt), e.hltbBest != null ? "HowLongToBeat" : "Est. playtime", ""]);
  const units = salesOf(row);
  if (units != null) cells.push([fmtUnits(units), "Units sold", ""]);
  const cv = collectionValueOf(row);
  if (cv != null) cells.push(["$" + cv.toFixed(0), "Value", ""]);
  // What we think YOU'd score it — only for games you haven't rated.
  const pred = typeof predictedCached === "function" ? predictedCached(row) : null;
  if (pred) cells.push([`~${Math.round(pred.score * 100)}`, "Predicted", ratingClass(pred.score)]);
  if (!cells.length) return "";
  return `<div class="hero-stats">` + cells.slice(0, 6).map(([v, l, cls]) =>
    `<div class="hero-stat"><b class="${cls}">${escapeHtml(String(v))}</b><span>${escapeHtml(l)}</span></div>`).join("") + `</div>`;
}

// Show the model's working. A prediction you can't interrogate is a horoscope.
// Each signal is a bar read against your own average, so it's obvious at a glance
// what pulled the number up and what dragged it down.
/* The prediction: a verdict, then the evidence.

   It used to be a four-column grid of tiny bars measured against a hairline you
   had to hover to identify, with values like "49 ×16" — model internals. The
   number is the most interesting thing on the card and it read like a debug view.

   The insight that reshaped it: "Compilation: 55" means nothing on its own. Is 55
   good? Only against YOUR average of 70. Every signal here is already a comparison,
   so print the comparison instead of making the reader compute it. */
function predictWhyHtml(row) {
  const p = typeof predictedCached === "function" ? predictedCached(row) : null;
  if (!p || !p.signals || !p.signals.length) return "";
  const base = p.baseline;
  const conf = p.confidence >= 0.75 ? "high" : p.confidence >= 0.5 ? "fair" : "low";
  const pts = (v) => Math.round(v * 100);
  const delta = (v) => pts(v) - pts(base);

  // What the number MEANS, in a sentence. The signals already say which way each
  // one pulls; the verdict is just the sum of them, said out loud.
  const up = p.signals.filter((sg) => sg.value >= base);
  const down = p.signals.filter((sg) => sg.value < base);
  // The named factors MUST agree with the verdict. The verdict is the sign of the
  // overall gap (below), so lead by the same sign — not by which group is larger.
  // Picking the bigger group let a below-average prediction cite your ABOVE-average
  // factors and claim you "rate them lower", which is exactly backwards.
  const gap = pts(p.score) - pts(base);
  const lead = gap >= 0 ? up : down;
  /* Only things YOU rate can go in "you rate X higher". The model also feeds on
     the critic score — kind "Critics", label "Metacritic" — and naming that here
     produced "You rate Metacritic higher than most of what you own", which is
     nonsense: you don't rate Metacritic, Metacritic rates the game. */
  const taste = lead.filter((sg) => sg.kind !== "Critics");
  const names = taste.slice(0, 2).map((sg) => sg.label).filter(Boolean);
  const critic = p.signals.find((sg) => sg.kind === "Critics");

  // Landing ON your average is not "better than your usual" — it's your usual.
  // Within a couple of points either way, the model is saying nothing much.
  let verdict;
  if (Math.abs(gap) <= 2) {
    verdict = `<b>About your usual.</b> Nothing here pulls it far from your ${pts(base)}% average.`;
  } else if (!names.length) {
    // Nothing but the critic score to go on — so say that, rather than inventing
    // a taste signal we don't have.
    verdict = critic
      ? `<b>${gap > 0 ? "Better" : "Below"} than your usual.</b> Little to go on beyond the critics, who gave it ${pts(critic.value)}.`
      : `<b>${gap > 0 ? "Better" : "Below"} than your usual.</b> Not much to go on for this one.`;
  } else {
    const list = names.join(" and ");
    verdict = gap > 0
      ? `<b>Better than your usual.</b> You rate ${list} higher than most of what you own.`
      : `<b>Below your usual.</b> You rate ${list} lower than most of what you own.`;
  }

  const rows = p.signals.map((sg) => {
    const d = delta(sg.value);
    // The critic score isn't a thing you've rated, so it doesn't get "N rated" and
    // it says who's doing the rating.
    const isCritic = sg.kind === "Critics";
    const label = isCritic
      ? `Critics <span>· ${escapeHtml(sg.label)} gave it ${pts(sg.value)}</span>`
      : `${escapeHtml(sg.label)}${sg.n ? ` <span>· ${sg.n} rated</span>` : ""}`;
    return `<div class="vd-r">
      <span class="vd-t">${label}</span>
      <span class="vd-d ${d >= 0 ? "up" : "dn"}">${d >= 0 ? "+" : "−"}${Math.abs(d)} vs your average</span>
    </div>`;
  }).join("");

  const m = typeof tasteModel === "function" ? tasteModel() : {};
  const err = m && m.eval && m.eval.mae != null ? ` Model error: ±${(m.eval.mae * 100).toFixed(1)} points.` : "";

  return `<div class="vd">
    <div class="vd-top">
      <span class="vd-num ${ratingClass(p.score)}">${pts(p.score)}<small>%</small></span>
      <span class="vd-side">
        <span class="vd-eye">${icon("i-trend", 12)} You'd probably rate this</span>
        <span class="vd-say">${verdict}</span>
      </span>
      <span class="vd-conf vd-${conf}">${conf} confidence</span>
    </div>
    <div class="vd-why">${rows}</div>
    <p class="vd-foot">Your average across ${(m.n || 0).toLocaleString()} rated games is ${pts(base)}%.${err}</p>
  </div>`;
}

function heroHtml(row, titleText) {
  const cs = coverSrc(ENRICH[row._k], "cover_big");
  const pixel = coverIsPixelArt(ENRICH[row._k], cs) ? " pixel" : "";
  const cover = cs
    ? `<img class="cover-big${pixel}" id="heroCover" src="${escapeHtml(cs)}" alt="">`
    : coverPending(row)
      ? `<div class="cover-big skel" id="heroCover"></div>`
      : `<div class="cover-big ph" id="heroCover">${icon("i-library", 40)}</div>`;
  const bits = [row.platform, row.releaseYear || row.releaseDate || row.release, row.genre]
    .filter((x) => x != null && x !== "")
    .map((x) => `<span class="pill facet-link" data-fk="${x === row.platform ? "platform" : x === row.genre ? "genre" : "releaseYear"}" data-fv="${escapeHtml(String(x))}">${escapeHtml(String(x))}</span>`);
  return `<div class="hero" id="drawerHero">
    <div class="hero-bg" id="heroBg"></div>
    <div class="hero-inner">
      ${cover}
      <div class="hero-txt">
        <h2>${titleText}</h2>
        <div class="subtitle">${bits.join("")}</div>
      </div>
    </div>
    <!-- Chips sit BELOW the cover+title row, not inside the text column: on a
         narrow screen they made that column taller than the cover, and the
         bottom-aligned cover then slid down past the title. -->
    <div id="heroChips"></div>
    ${heroStatsHtml(row)}
    ${predictWhyHtml(row)}
    ${launchHtml(row) ? `<div class="hero-actions">${launchHtml(row)}</div>` : ""}
  </div>`;
}

function igdbAttr(d) {
  const name = d.source || "IGDB";
  const link = d.url ? `<a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">View on ${escapeHtml(name)} ↗</a> · ` : "";
  const by = d.source
    ? `Metadata via ${escapeHtml(name)}`
    : `Metadata by <a href="https://www.igdb.com" target="_blank" rel="noopener">IGDB</a>`;
  return `<div class="igdb-attr">${link}${by}</div>`;
}

function mapControlHtml(key) {
  // Current mapping per source, so the boxes show what a game is already matched to.
  const d = DETAIL[key] || {};
  const primary = d.source ? String(d.source).toLowerCase() : (d.igdbId ? "igdb" : null);
  const cur = {
    igdb: primary === "igdb" ? d.url || "" : "",
    steam: primary === "steam" ? d.url || "" : "",
    ign: primary === "ign" ? d.url || "" : "",
    launchbox: primary === "launchbox" ? d.url || "" : "",
    hltb: (HLTBC[key] || {}).url || "",
    metacritic: (MCC[key] || {}).url || "",
    gameye: (GEC[key] || {}).url || "",
    arcadedb: (ADBC[key] || {}).url || "",
    vndb: (VNC[key] || {}).url || "",
    vgchartz: (VGC[key] || {}).url || "",
    steamx: (SXC[key] || {}).url || "",
    speedrun: (SRC[key] || {}).url || "",
    guides: (GDC[key] || {}).url || "",
  };
  // IGDB / Steam / IGN all fill the same "primary metadata" slot.
  const rows = [
    { id: "igdb", label: "Metadata — IGDB", ph: "IGDB game URL" },
    { id: "steam", label: "Metadata — Steam", ph: "Steam store URL (…/app/<id>/)" },
    { id: "ign", label: "Metadata — IGN", ph: "IGN game URL" },
    { id: "launchbox", label: "Metadata — LaunchBox", ph: "LaunchBox game URL" },
  ];
  if (ENRICH_SOURCES.includes("hltb")) rows.push({ id: "hltb", label: "HowLongToBeat", ph: "HLTB game URL" });
  if (ENRICH_SOURCES.includes("metacritic")) rows.push({ id: "metacritic", label: "Metacritic", ph: "Metacritic game URL" });
  const ownedPhys = drawerRow && drawerRow.owned && (drawerRow.format || "").toLowerCase() === "physical";
  if (ENRICH_SOURCES.includes("gameye") && ownedPhys) rows.push({ id: "gameye", label: "GameEye value", ph: "GameEye encyclopedia URL" });
  // Gated sources: only offer the mapping box where the source could apply.
  if (ENRICH_SOURCES.includes("arcadedb") && (drawerRow || {}).mameRomset) rows.push({ id: "arcadedb", label: "Arcade Database", ph: "adb.arcadeitalia.net/?mame=<romset>" });
  if (ENRICH_SOURCES.includes("vndb") && ["Visual Novel", "Adventure"].includes((drawerRow || {}).genre)) rows.push({ id: "vndb", label: "VNDB", ph: "vndb.org/v<id>" });
  if (ENRICH_SOURCES.includes("vgchartz")) rows.push({ id: "vgchartz", label: "VGChartz sales", ph: "vgchartz.com/games/game.php?id=<id>" });
  // Steam extras are keyed on the appid, so mapping means pointing at the store page.
  if (ENRICH_SOURCES.includes("steamx")) rows.push({ id: "steamx", label: "Steam Deck / ProtonDB", ph: "store.steampowered.com/app/<appid>/" });
  if (ENRICH_SOURCES.includes("speedrun")) rows.push({ id: "speedrun", label: "speedrun.com", ph: "speedrun.com/<game>" });
  if (ENRICH_SOURCES.includes("cooptimus") && COOP_PLATFORMS.has((drawerRow || {}).platform))
    rows.push({ id: "cooptimus", label: "Co-Optimus", ph: "co-optimus.com/game/<id>/..." });
  if (ENRICH_SOURCES.includes("guides")) rows.push({ id: "guides", label: "StrategyWiki guide", ph: "strategywiki.org/wiki/<Page>" });
  return `<details class="map-menu"><summary>${icon("i-edit", 13)} Fix mapping</summary>` +
    rows.map((s) => `<div class="map-src" data-src="${s.id}"><label>${escapeHtml(s.label)}</label>
      <div class="map-row"><input type="url" placeholder="${s.ph}" value="${escapeHtml(cur[s.id] || "")}" data-map-input>
      <button class="btn" data-map-go>Map</button>
      <button class="linkbtn" data-map-reset title="Re-run auto-matching">Auto</button>
      <button class="linkbtn danger" data-map-remove title="Pin as no match — auto-matching won't re-fill it">Remove</button>
      </div></div>`).join("") +
    `</details>`;
}

function hltbHtml(h) {
  const est = drawerRow ? drawerRow.estimatedTime : null;
  if (h) {
    const rows = [["Main Story", h.main], ["Main + Extras", h.mainPlus], ["Completionist", h.hundred], ["All Styles", h.allStyles]]
      .filter(([, v]) => v != null);
    if (!rows.length && est == null) return "";
    return `<div class="hltb"><div class="hltb-head">⏱ HowLongToBeat</div>` +
      rows.map(([l, v]) => `<div class="hltb-row"><span>${l}</span><b>${fmtHours(v)}</b></div>`).join("") +
      (h.url ? `<a class="hltb-link" href="${escapeHtml(h.url)}" target="_blank" rel="noopener">View on HowLongToBeat ↗</a>` : "") +
      `</div>`;
  }
  if (est != null)
    return `<div class="hltb"><div class="hltb-head">⏱ Playtime</div><div class="hltb-row"><span>Estimated (from sheet)</span><b>${fmtHours(est)}</b></div></div>`;
  return "";
}

// ---- media carousel + lightbox ------------------------------------------
// Slides are trailers and stills. A trailer autoplays (muted) as soon as it's
// the visible slide — it's the first thing you see when you open a game.
let media = [], shotIds = [], shotIdx = 0, lbIdx = 0;
function wireCarousel(el, items) {
  const wrap = el.querySelector(".shots");
  media = items || [];
  shotIds = media.filter((m) => m.kind === "image").map((m) => m.id);   // lightbox = stills only
  if (!wrap || !media.length) return;
  const view = wrap.querySelector(".shot-view");
  const count = wrap.querySelector(".shot-count");
  const cap = wrap.querySelector(".shot-cap");

  const show = (i) => {
    shotIdx = (i + media.length) % media.length;
    const m = media[shotIdx];
    view.innerHTML = "";
    if (m.kind === "video") {
      if (YT_BLOCKED) { view.appendChild(ytFallback(m)); }
      else {
        const frame = document.createElement("iframe");
        frame.className = "shot-video";
        // muted: a browser will refuse to autoplay with sound, and it would be
        // rude anyway. Controls are on, so it can be unmuted.
        frame.src = ytSrc(m.id, { autoplay: "1", mute: "1" });
        frame.allow = "accelerometer; autoplay; encrypted-media; picture-in-picture";
        frame.allowFullscreen = true;
        frame.title = m.name;
        view.appendChild(frame);
        // If it never plays (YouTube's bot wall, or embedding disabled), swap in
        // a thumbnail that opens the video on YouTube — where the user can
        // actually sign in, or just watch it.
        ytWatch(frame, () => {
          if (frame.isConnected) frame.replaceWith(ytFallback(m));
        });
      }
    } else {
      const img = document.createElement("img");
      img.className = "shot-img";
      img.loading = "lazy";
      img.alt = "";
      img.src = IMG(m.id, m.art ? "1080p" : "screenshot_med");
      img.onclick = () => openLightbox(shotIds.indexOf(m.id));
      view.appendChild(img);
    }
    count.textContent = `${shotIdx + 1} / ${media.length}`;
    // A video always carries a link out to YouTube — the embed can be walled by
    // YouTube's per-device bot check (not something we can talk our way past), and a
    // walled iframe with no way to click through is worse than no video.
    if (m.kind === "video")
      cap.innerHTML = `${escapeHtml(m.name)} · <a href="https://www.youtube.com/watch?v=${escapeHtml(m.id)}" target="_blank" rel="noopener">Watch on YouTube ↗</a>`;
    else cap.textContent = m.art ? "Artwork" : "";
  };

  const prev = wrap.querySelector(".prev"), next = wrap.querySelector(".next");
  if (prev) prev.onclick = (e) => { e.stopPropagation(); show(shotIdx - 1); };
  if (next) next.onclick = (e) => { e.stopPropagation(); show(shotIdx + 1); };
  show(0);
}
// A clickable poster that opens the trailer on YouTube in a new tab.
function ytFallback(m) {
  const a = document.createElement("a");
  a.className = "shot-fallback";
  a.href = `https://www.youtube.com/watch?v=${m.id}`;
  a.target = "_blank";
  a.rel = "noopener";
  a.innerHTML =
    `<img src="https://i.ytimg.com/vi/${escapeHtml(m.id)}/hqdefault.jpg" alt="">
     <span class="shot-fallback-play">▶</span>
     <span class="shot-fallback-note">YouTube won’t play this here — watch it on YouTube ↗</span>`;
  return a;
}

function lbShow(delta) {
  if (!shotIds.length) return;
  lbIdx = (lbIdx + delta + shotIds.length) % shotIds.length;
  $("#lbImg").src = IMG(shotIds[lbIdx], "screenshot_huge");
  $("#lbCount").textContent = `${lbIdx + 1} / ${shotIds.length}`;
  const multi = shotIds.length > 1;
  $("#lbPrev").hidden = !multi;
  $("#lbNext").hidden = !multi;
}
function openLightbox(i) { lbIdx = i; $("#lightbox").hidden = false; lbShow(0); syncScrollLock(); }
function closeLightbox() { $("#lightbox").hidden = true; syncScrollLock(); }
const lightboxOpen = () => !$("#lightbox").hidden;

function metacriticHtml(key) {
  const mc = MCC[key];
  const sheet = drawerRow ? drawerRow.metacriticRating : null;
  const scraped = mc && mc.metascore != null;
  const score = scraped ? mc.metascore : (sheet != null ? Math.round(sheet * 100) : null);
  if (score == null) return "";
  const src = scraped
    ? (mc.url ? `<a href="${escapeHtml(mc.url)}" target="_blank" rel="noopener">Metacritic ↗</a>` : "Metacritic")
    : "from sheet";
  return `<div class="hltb"><div class="hltb-row"><span>Metacritic</span>` +
    `<b class="${ratingClass(score / 100)}">${score} <small class="muted">· ${src}</small></b></div></div>`;
}

function gameyeHtml(key) {
  const ge = GEC[key];
  if (!ge) return "";
  const rows = [["Loose", ge.priceLoose], ["CIB", ge.priceCib], ["New", ge.priceNew]].filter(([, v]) => v != null);
  if (!rows.length) return "";
  const cond = (drawerRow && drawerRow.condition) || "";
  const key2 = { complete: "priceCib", cib: "priceCib", loose: "priceLoose", new: "priceNew" }[cond.toLowerCase()] || "priceLoose";
  const qty = quantityFromNotes(drawerRow && drawerRow.notes);
  let mine = "";
  if (ge[key2] != null) {
    const total = ge[key2] * qty;
    mine = `<div class="hltb-row mine"><span>Your copy${qty > 1 ? ` ×${qty}` : ""}${cond ? ` (${escapeHtml(cond)})` : ""}</span><b>$${total.toFixed(2)}</b></div>`;
  }
  return `<div class="hltb"><div class="hltb-head">${icon("i-trend", 15)} Value (GameEye)</div>` +
    rows.map(([l, v]) => `<div class="hltb-row"><span>${l}</span><b>$${v.toFixed(2)}</b></div>`).join("") + mine +
    (ge.url ? `<a class="hltb-link" href="${escapeHtml(ge.url)}" target="_blank" rel="noopener">View on GameEye ↗</a>` : "") +
    `</div>`;
}

// Arcade Database: cabinet/marquee scans plus the cabinet's own specs. Matched
// on the MAME romset, so if it's here at all it's the right machine.
function arcadeHtml(key) {
  const a = ADBC[key];
  if (!a) return "";
  const shots = [["Cabinet", a.cabinet], ["Marquee", a.marquee], ["Flyer", a.flyer], ["Title", a.titleScreen]]
    .filter(([, u]) => u)
    .map(([l, u]) => `<figure class="adb-art"><img loading="lazy" src="${escapeHtml(u)}" alt="${l}"><figcaption>${l}</figcaption></figure>`)
    .join("");
  const spec = [
    ["Players", a.playersDetail || (a.players != null ? String(a.players) : null)],
    ["Controls", a.controls], ["Buttons", a.buttons != null ? String(a.buttons) : null],
    ["Screen", [a.orientation, a.resolution].filter(Boolean).join(" · ") || null],
    ["Manufacturer", a.manufacturer], ["Year", a.year],
  ].filter(([, v]) => v)
    .map(([l, v]) => `<div class="hltb-row"><span>${l}</span><b>${escapeHtml(String(v))}</b></div>`).join("");
  return `<div class="hltb"><div class="hltb-head">${icon("i-dice", 15)} Arcade cabinet${a.romset ? ` <span class="muted">${escapeHtml(a.romset)}</span>` : ""}</div>` +
    (shots ? `<div class="adb-arts">${shots}</div>` : "") + spec +
    (a.history ? `<details class="adb-history"><summary>MAME history</summary><p>${escapeHtml(a.history)}</p></details>` : "") +
    (a.url ? `<a class="hltb-link" href="${escapeHtml(a.url)}" target="_blank" rel="noopener">View on Arcade Database ↗</a>` : "") +
    `</div>`;
}

function vndbHtml(key) {
  const v = VNC[key];
  if (!v) return "";
  const rows = [
    ["Rating", v.rating != null ? `${Math.round(v.rating * 100)}%${v.votes ? ` (${v.votes.toLocaleString()} votes)` : ""}` : null],
    ["Median length", v.hours != null ? fmtHours(v.hours) : null],
    ["Released", v.released || null],
  ].filter(([, x]) => x)
    .map(([l, x]) => `<div class="hltb-row"><span>${l}</span><b>${escapeHtml(String(x))}</b></div>`).join("");
  if (!rows) return "";
  return `<div class="hltb"><div class="hltb-head">${icon("i-review", 15)} Visual novel (VNDB)</div>${rows}` +
    (v.url ? `<a class="hltb-link" href="${escapeHtml(v.url)}" target="_blank" rel="noopener">View on VNDB ↗</a>` : "") +
    `</div>`;
}

function salesHtml(key) {
  const v = VGC[key];
  if (!v || v.units == null) return "";
  const rows = [["Shipped", v.shipped], ["Sold", v.sold]].filter(([, x]) => x != null)
    .map(([l, x]) => `<div class="hltb-row"><span>${l}</span><b>${x.toLocaleString()}</b></div>`).join("");
  return `<div class="hltb"><div class="hltb-head">${icon("i-trend", 15)} Sales (VGChartz)</div>${rows}` +
    `<div class="hltb-note muted">VGChartz estimate${v.console ? ` · ${escapeHtml(v.console)}` : ""}</div>` +
    (v.url ? `<a class="hltb-link" href="${escapeHtml(v.url)}" target="_blank" rel="noopener">View on VGChartz ↗</a>` : "") +
    `</div>`;
}

// Thumby/Thumby Color: TinyCircuits' list is the only place these exist.
function thumbyHtml(key) {
  const t = THC[key];
  if (!t) return "";
  const art = [["Title", t.titleImage], ["Icon", t.icon]].filter(([, u]) => u)
    .map(([l, u]) => `<figure class="adb-art"><img class="pixel" loading="lazy" src="${escapeHtml(u)}" alt="${l}"><figcaption>${l}</figcaption></figure>`)
    .join("");
  // Tinymine and Thoom ship no still image at all — only an animated title
  // card. Show it, so they aren't blank.
  const vid = t.video
    ? `<video class="thumby-vid" src="${escapeHtml(t.video)}" autoplay muted loop playsinline></video>` : "";
  return `<div class="hltb"><div class="hltb-head">${icon("i-target", 15)} ${escapeHtml(t.platform || "Thumby")}</div>` +
    (art ? `<div class="adb-arts">${art}</div>` : "") + vid +
    (t.description ? `<p class="thumby-desc">${escapeHtml(t.description)}</p>` : "") +
    (t.url ? `<a class="hltb-link" href="${escapeHtml(t.url)}" target="_blank" rel="noopener">View on GitHub ↗</a>` : "") +
    `</div>`;
}

// Steam extras — all keyed on the appid, so if they're here they're right.
/* Co-op. IGDB says "co-operative" and stops; this says whether that's two people
   on one sofa or eight strangers online — the only part that decides what you
   actually play tonight. */
function coopHtml(key) {
  const c = COOPC[key];
  if (!c) return "";
  const rows = [];
  if (c.localPlayers > 1) {
    rows.push(`<div class="hltb-row"><span>On one screen</span><b class="good">${c.localPlayers} players${c.splitscreen ? " · splitscreen" : ""}</b></div>`);
  }
  if (c.onlinePlayers > 1) rows.push(`<div class="hltb-row"><span>Online</span><b>${c.onlinePlayers} players</b></div>`);
  if (c.lanPlayers > 1) rows.push(`<div class="hltb-row"><span>LAN</span><b>${c.lanPlayers} players</b></div>`);
  if (c.campaignCoop) rows.push(`<div class="hltb-row"><span>Campaign</span><b class="good">Playable co-op</b></div>`);
  if (c.dropIn) rows.push(`<div class="hltb-row"><span>Drop-in</span><b>Join mid-game</b></div>`);
  if (!rows.length) return "";
  const link = c.url
    ? `<a class="hltb-link" href="${escapeHtml(c.url)}" target="_blank" rel="noopener">View on Co-Optimus ↗</a>` : "";
  return `<div class="hltb"><div class="hltb-head">${icon("i-star", 15)} Co-op (Co-Optimus)</div>${rows.join("")}${link}</div>`;
}

function steamxHtml(key) {
  const x = SXC[key];
  if (!x) return "";
  const deck = x.deck
    ? `<div class="hltb-row"><span>Steam Deck</span><b class="deck deck-${escapeHtml(String(x.deck).toLowerCase())}">${escapeHtml(x.deck)}</b></div>` : "";
  const proton = x.protonTier
    ? `<div class="hltb-row"><span>ProtonDB</span><b class="proton proton-${escapeHtml(String(x.protonTier))}">${escapeHtml(x.protonTier)}${x.protonReports ? ` <span class="muted">(${x.protonReports} reports)</span>` : ""}</b></div>` : "";
  const rev = x.reviewScore != null
    ? `<div class="hltb-row"><span>Steam reviews</span><b class="${ratingClass(x.reviewScore)}">${Math.round(x.reviewScore * 100)}% positive <span class="muted">of ${(x.positive + x.negative).toLocaleString()}</span></b></div>` : "";
  const own = x.owners
    ? `<div class="hltb-row"><span>Owners (est.)</span><b>${escapeHtml(x.owners)}</b></div>` : "";
  const ccu = x.concurrent
    ? `<div class="hltb-row"><span>Playing now</span><b>${x.concurrent.toLocaleString()}</b></div>` : "";
  const a = x.achievements;
  const ach = a
    ? `<div class="hltb-row"><span>Achievements</span><b>${a.count} <span class="muted">· median ${a.medianPercent}% · rarest ${a.rarestPercent}%</span></b></div>` : "";
  if (!(deck || proton || rev || own || ccu || ach)) return "";
  return `<div class="hltb"><div class="hltb-head">${icon("i-play", 15)} Steam</div>${deck}${proton}${rev}${own}${ccu}${ach}` +
    (x.protonUrl ? `<a class="hltb-link" href="${escapeHtml(x.protonUrl)}" target="_blank" rel="noopener">View on ProtonDB ↗</a>` : "") +
    `</div>`;
}

// The world record, next to HowLongToBeat: a nice sense of scale.
function speedrunHtml(key) {
  const r = SRC[key];
  if (!r || !r.wrTime) return "";
  const rows = (r.categories || []).slice(0, 3).map((c) =>
    `<div class="hltb-row"><span>${escapeHtml(c.category)}</span><b>${escapeHtml(c.time)}</b></div>`).join("");
  return `<div class="hltb"><div class="hltb-head">${icon("i-trophy", 15)} World records</div>${rows}` +
    (r.url ? `<a class="hltb-link" href="${escapeHtml(r.url)}" target="_blank" rel="noopener">Leaderboards on speedrun.com ↗</a>` : "") +
    `</div>`;
}

function guidesHtml(key) {
  const g = GDC[key];
  if (!g) return "";
  const secs = (g.sections || []).slice(0, 6)
    .map((x) => `<span class="chip">${escapeHtml(x)}</span>`).join("");
  return `<div class="hltb"><div class="hltb-head">${icon("i-review", 15)} Guide (StrategyWiki)</div>` +
    (secs ? `<div class="chips">${secs}</div>` : "") +
    `<a class="hltb-link" href="${escapeHtml(g.url)}" target="_blank" rel="noopener">${g.hasWalkthrough ? "Read the walkthrough" : "Open the guide"} ↗</a></div>`;
}

// Compose the drawer's enrichment section: IGDB + HLTB + Metacritic + GameEye + map.
// Push the full detail up into the hero: a screenshot becomes the backdrop, the
// cover sharpens, the IGDB score and chips appear.
function fillHero(detail) {
  const bg = $("#heroBg"), coverEl = $("#heroCover"), chipsEl = $("#heroChips");
  if (!detail) return;
  // Artwork first: it's cinematic key art, made to be looked at. A screenshot is
  // a fallback — it's a picture of a HUD. Ask for the big version: this is a
  // full-bleed banner now, not a blurred wash, so a low-res source would show.
  const art = (detail.artworks || [])[0] || (detail.screenshots || [])[0];
  if (bg && art) {
    const img = new Image();          // fade it in only once it's actually there
    img.onload = () => {
      bg.style.backgroundImage = `url("${IMG(art, "1080p")}")`;
      bg.classList.add("on");
    };
    img.src = IMG(art, "1080p");
  }
  const cs = coverSrc(detail, "cover_big");
  if (coverEl && cs && coverEl.tagName !== "IMG") {
    const img = document.createElement("img");
    img.className = "cover-big"; img.id = "heroCover"; img.alt = ""; img.src = cs;
    coverEl.replaceWith(img);
  }
  // Chips moved out of the hero: they crowded the cover and title, and the IGDB
  // score chip repeated the "Players" figure already in the stat strip. They live
  // under the summary now, which is where you're reading about the game anyway.
  if (chipsEl) chipsEl.innerHTML = "";
}

function renderIgdbSection(key, el, status, detail) {
  // The relationship map is about YOUR collection, not IGDB's copy of the game,
  // so it's painted into its own host rather than the enrichment block.
  const relHost = $("#relations");
  if (relHost && status === "matched" && detail) {
    relHost.innerHTML = relationsHtml(detail);
    wireRelations(relHost);
    // IGDB wins, the sheet Collection is the fallback: once IGDB confirms a grouping
    // (episodes, a bundle's games, DLC…), fold away the in-house collection section so
    // the same set isn't shown twice. It rendered synchronously; IGDB detail is async.
    if (typeof relationsHaveGrouping === "function" && relationsHaveGrouping(detail)) {
      const colSec = document.querySelector("#drawerBody .col-section");
      if (colSec) colSec.hidden = true;
    }
  }
  let content;
  if (status === "matched" && detail) { content = detailHtml(detail); fillHero(detail); }
  else if (status === "no_match") content = `<div class="igdb-loading muted">No IGDB match for this title.</div>`;
  else {
    // Loading / pending / error. The hero already carries the cover and title,
    // so this is only the prose area — shimmer lines, never a bare "Loading".
    const msg = status === "pending-final" ? "Metadata still resolving — reopen shortly."
      : status === "error" ? "Couldn’t load extra details." : "";
    content = msg
      ? `<div class="igdb-loading muted">${msg}</div>`
      : `<div class="skel skel-line" style="height:18px;width:40%"></div>
         <div class="skel skel-line"></div><div class="skel skel-line"></div>
         <div class="skel skel-line short"></div>`;
  }
  el.innerHTML = content + hltbHtml(HLTBC[key]) + speedrunHtml(key) + metacriticHtml(key)
    + coopHtml(key) + steamxHtml(key) + arcadeHtml(key) + vndbHtml(key) + thumbyHtml(key) + guidesHtml(key)
    + salesHtml(key) + gameyeHtml(key) + mapControlHtml(key);

  el.querySelectorAll(".map-src").forEach((rowEl) => {
    const src = rowEl.dataset.src;
    const go = rowEl.querySelector("[data-map-go]");
    const input = rowEl.querySelector("[data-map-input]");
    const reset = rowEl.querySelector("[data-map-reset]");
    const remove = rowEl.querySelector("[data-map-remove]");
    const submit = async () => {
      const url = input.value.trim();
      if (!url) return;
      go.disabled = true; go.textContent = "…"; input.classList.remove("err");
      const ok = await submitOverride(key, url, src);
      go.disabled = false; go.textContent = "Map";
      if (ok) loadDetail(key, el); else input.classList.add("err");
    };
    go.onclick = submit;
    input.onkeydown = (e) => { if (e.key === "Enter") submit(); };
    reset.onclick = async () => { await submitOverride(key, "", src); loadDetail(key, el); };
    remove.onclick = async () => {
      await submitOverride(key, "", src, true);   // pin as no match
      input.value = "";
      loadDetail(key, el);
    };
  });

  wireCarousel(el, detail ? mediaOf(detail) : []);
  el.querySelectorAll("[data-simk]").forEach((btn) => {
    btn.onclick = () => {
      const row = ((DATA.sheets.games || {}).rows || []).find((r) => String(r._k) === btn.dataset.simk);
      if (row) openDrawerFrom(row, "games");        // navigation: keep a way back
    };
  });
}

async function submitOverride(key, url, source = "igdb", remove = false) {
  try {
    const res = await fetch("api/enrichment/override", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, url, source, remove }),
    });
    if (!res.ok) return false;
    const j = await res.json();
    // Clear caches so the refetch shows the new mapping.
    delete DETAIL[key]; delete HLTBC[key]; delete MCC[key]; delete GEC[key];
    if (["igdb", "steam", "ign", "gamespot"].includes(source)) {   // primary slot
      const r = remove ? null : j.record;
      if (r) ENRICH[key] = Object.assign(ENRICH[key] || {}, {
        cover: r.cover, coverUrl: r.coverUrl, source: r.source, igdbId: r.igdbId,
        genres: r.genres, themes: r.themes, gameModes: r.gameModes, userRating: r.userRating,
      });
      else delete ENRICH[key];
    }
    renderTable(currentFiltered);   // refresh cover/facets on the grid
    return true;
  } catch (_) { return false; }
}

async function loadDetail(key, el, attempt = 0, row = null) {
  if (row) drawerRow = row;
  if (DETAIL[key]) { renderIgdbSection(key, el, "matched", DETAIL[key]); return; }
  if (attempt === 0) renderIgdbSection(key, el, "loading", null);
  try {
    const res = await fetch("api/enrichment/detail?key=" + encodeURIComponent(key));
    const j = await res.json();
    if ("hltb" in j) HLTBC[key] = j.hltb;
    if ("metacritic" in j) MCC[key] = j.metacritic;
    if ("gameye" in j) GEC[key] = j.gameye;
    if ("arcadedb" in j) ADBC[key] = j.arcadedb;
    if ("vndb" in j) VNC[key] = j.vndb;
    if ("vgchartz" in j) VGC[key] = j.vgchartz;
    if ("thumby" in j) THC[key] = j.thumby;
    if ("steamx" in j) SXC[key] = j.steamx;
    if ("speedrun" in j) SRC[key] = j.speedrun;
    if ("guides" in j) GDC[key] = j.guides;
    if ("cooptimus" in j) COOPC[key] = j.cooptimus;
    if (j.status === "matched" && j.detail) { DETAIL[key] = j.detail; renderIgdbSection(key, el, "matched", j.detail); }
    else if (j.status === "no_match") { renderIgdbSection(key, el, "no_match", null); }
    else if (j.status === "pending") {
      if (attempt >= 15) renderIgdbSection(key, el, "pending-final", null);
      else setTimeout(() => loadDetail(key, el, attempt + 1), 2500);
    } else renderIgdbSection(key, el, "error", null);
  } catch (_) { renderIgdbSection(key, el, "error", null); }
}

// Bulk-load the light cover/facet map for every already-matched game (powers
// covers + IGDB facets across the whole list). Re-polls while backfill runs.
let allTimer = null;
async function loadAllEnrichment() {
  if (!ENRICH_ENABLED) return;
  try {
    const res = await fetch("api/enrichment/all");
    const j = await res.json();
    if (j.enabled === false) { ENRICH_ENABLED = false; return; }
    let changed = false;
    for (const [k, v] of Object.entries(j.items || {})) {
      ENRICH[k] = Object.assign(ENRICH[k] || {}, v);
      changed = true;
    }
    // The rows we looked up and found nothing for. They will never get a cover, so
    // they must stop pretending one is on the way.
    if (j.noMatch) {
      const before = NO_MATCH.size;
      NO_MATCH = new Set(j.noMatch);
      if (NO_MATCH.size !== before) changed = true;
    }
    if (j.stats) updateEnrichStatus(j.stats);
    if (changed) {
      _enrichEpoch++; resetSearchCache();     // IGDB genres just became searchable
      // Several health checks read the enrichment map (missing metadata, HLTB
      // mismatches), and its results are cached — so they must be recomputed
      // once enrichment lands, or "no metadata" reads as "all 14,747 games".
      resetHealth();
      // Patch in place rather than re-rendering (which would flicker every image).
      if (activeTab === "stats") renderStats();
      else if (activeTab === "home") patchHomeCovers();   // in place: a full re-render flickers
      else if (activeTab === "challenges") renderChallenges();
      else if (activeTab === "health") renderHealth();
      else if (activeTab === "groups") patchGroupCovers();
      else if (activeTab !== "pick") {
        patchEnrichedCells();
        patchTimelineCovers();          // the Completed tab's third view
        renderFacets();
        // Grouping keys off the IGDB id, which lives in the enrichment map — and
        // the grid paints before that map arrives, so the first render has
        // nothing to group by. Re-render, but only when the grouping actually
        // changes, or we'd flash the grid on every poll.
        if (!SPECIAL_TABS.includes(activeTab) && combineOn()) {
          resetRelations();
          const n = groupByGame(currentFiltered).length;
          if (n !== lastGroupedCount) { lastGroupedCount = n; renderTable(currentFiltered); }
        }
      }
    }
    if (j.stats && !j.stats.complete) {             // a backfill is still running
      clearTimeout(allTimer);
      allTimer = setTimeout(loadAllEnrichment, 45000);
    }
  } catch (_) { /* transient */ }
}

// ---- data access --------------------------------------------------------
const sheet = () => DATA.sheets[activeTab];
const columns = () => sheet().columns;
const searchCols = () => columns().filter((c) => c.search).map((c) => c.key);
const colByKey = (key) => columns().find((c) => c.key === key);

// The 13 platforms Co-Optimus covers (see src/cooptimus.py).
const COOP_PLATFORMS = new Set(["Nintendo Switch", "Nintendo Wii U", "PC", "PlayStation 2",
  "PlayStation 3", "PlayStation 4", "PlayStation 5", "Nintendo Wii", "WiiWare", "Xbox",
  "Xbox 360", "Xbox One", "Xbox Series X|S"]);

const titleCase = (s) => String(s).replace(/\b[a-z]/g, (c) => c.toUpperCase());

/* Priority is a LABEL now, not a number — so sorting or listing it alphabetically
   would put "Might Play" above "Must Play" above "Want to Play", which is
   meaningless. Rank it by intent, which is what the number meant. */
const PRIORITY_RANK = {
  "Must Play": 5, "Will Play": 4, "Want to Play": 3, "Might Play": 2, "Will Not Play": 1,
};
const priorityRank = (v) => PRIORITY_RANK[v] ?? 0;

/* One search field. There were three — the top bar, Groupings and Reviews — each
   styled separately and drifting apart. Same markup everywhere now, so the icon,
   the height, the radius and the focus ring can't disagree. */
function searchField(id, placeholder, value = "", cls = "") {
  return `<span class="field ${cls}">
    <svg class="ico" width="15" height="15" aria-hidden="true"><use href="#i-search"/></svg>
    <input id="${id}" type="search" placeholder="${escapeHtml(placeholder)}"
      value="${escapeHtml(String(value))}" autocomplete="off" spellcheck="false">
  </span>`;
}

/* Sort keys that aren't sheet columns. The estimated rating is computed in the
   browser (ridge regression over your own ratings), so there is no cell to sort
   on — cmpBy has to be told how to get the value instead of reading a[key]. */
const VIRTUAL_SORTS = [
  { key: "__predicted", label: "Estimated Rating", type: "number", kind: "predicted",
    get: (row) => (typeof predictedOf === "function" ? predictedOf(row) : null),
    on: () => activeTab === "games" },
  // These three have fallback chains, which is exactly why they can't be plain
  // columns: the best answer lives in a different source per game. The facets
  // already resolve them, so sorting reuses the same accessors rather than
  // inventing a second, divergent answer.
  { key: "__critic", label: "Critic Rating", type: "number", kind: "critic",
    get: (row) => metacriticOf(row),        // Metacritic scrape → sheet's column
    on: () => activeTab === "games" },
  { key: "__user", label: "User Rating", type: "number", kind: "user",
    get: (row) => userRatingOf(row),        // IGDB → VNDB → GameFAQs
    on: () => activeTab === "games" },
  { key: "__esttime", label: "Estimated Time", type: "number", kind: "esttime",
    get: (row) => playtimeOf(row),          // HLTB → VNDB → the sheet's estimate
    on: () => activeTab === "games" },
];
const sortMeta = (key) => VIRTUAL_SORTS.find((v) => v.key === key) || colByKey(key);

/* The sort menu on All Games. Every sortable column used to be offered — thirty-odd
   options, most of which nobody would ever sort by (File Size, MAME Romset, English).
   A curated list of the ones that answer a real question, in the order you'd reach
   for them: what it is, what it's worth, what it cost, when you played it. */
const GAMES_SORT_MENU = [
  "title", "platform", "releaseDate",
  "rating", "__critic", "__user", "__predicted",
  "priority",
  "datePurchased", "purchasePrice",
  "dateStarted", "dateCompleted", "completionTime", "__esttime",
];
// The sheet's own headers read as filing-cabinet labels ("Date Purchased"); in a
// sort menu you want the thing first.
const SORT_LABEL = {
  rating: "Rating (yours)",
  datePurchased: "Purchased Date",
  dateStarted: "Started Date",
  dateCompleted: "Completed Date",
};
const sortLabel = (c) => SORT_LABEL[c.key] || c.label;

// Virtual facets sourced from IGDB enrichment (array-valued, joined via row._k).
// Genre is NOT here — it's unified with the sheet's Genre facet (see unifiedFacetCol).
const IGDB_FACET_DEFS = [
  { key: "__igdb_theme", label: "Theme", source: "themes" },
  { key: "__igdb_mode", label: "Game Mode", source: "gameModes" },
];

/* ---- unifying sheet + IGDB for developer / publisher / franchise / genre ----
 * The sheet holds ONE value per field; IGDB holds many. We join them into one
 * multi-valued facet so a game is filed under every developer/publisher/franchise/
 * genre either source knows — and a value that only IGDB knows (a co-developer not
 * in your sheet) is a real, clickable facet value that filters all the same.
 *
 * External names are mapped onto your sheet's spelling wherever they match; genres,
 * where the sheet is finer-grained than IGDB, roll up into shared umbrellas. */

// Company names: fold to a comparable key (case, punctuation, Inc/Ltd/Co, "the").
// Memoised — called once per row per facet-count pass, but only a few thousand
// distinct strings exist across the whole library.
const _ncCache = new Map();
function normCompany(s) {
  s = String(s || "");
  let v = _ncCache.get(s);
  if (v !== undefined) return v;
  v = s.toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[.,'’]/g, "")
    .replace(/\b(inc|incorporated|ltd|limited|llc|co|corp|corporation|company|gmbh|kk|sa|srl|pty|the)\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
  _ncCache.set(s, v);
  return v;
}
// Franchise/series titles: lighter touch — drop punctuation, "the", trailing "series".
const _nfCache = new Map();
function normFranchise(s) {
  s = String(s || "");
  let v = _nfCache.get(s);
  if (v !== undefined) return v;
  v = s.toLowerCase()
    .replace(/[.,:'’!?]/g, "")
    .replace(/\bthe\b/g, " ").replace(/\bseries\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
  _nfCache.set(s, v);
  return v;
}

// IGDB genre label -> your sheet's spelling, where one clean value exists. Where the
// sheet is only granular (no plain "Platformer"/"RPG"), the alias IS the umbrella that
// the granular sheet genres roll up into (see genreUmbrellas).
const GENRE_ALIAS = {
  "role-playing (rpg)": "RPG", "rpg": "RPG",
  "platform": "Platformer",
  "shooter": "Shooter", "strategy": "Strategy", "adventure": "Adventure",
  "real time strategy (rts)": "Real-Time Strategy",
  "turn-based strategy (tbs)": "Turn-Based Strategy",
  "hack and slash/beat 'em up": "Beat 'em Up",
  "simulator": "Simulation", "sport": "Sports", "music": "Rhythm",
  "quiz/trivia": "Trivia", "card & board game": "Board Game",
  "point-and-click": "Point-and-Click",
  "puzzle": "Puzzle", "fighting": "Fighting", "racing": "Racing",
  "visual novel": "Visual Novel", "arcade": "Arcade", "pinball": "Pinball",
  "indie": "Indie", "moba": "MOBA", "tactical": "Tactical",
  "action": "Action", "compilation": "Compilation",
};
const canonGenre = (raw) => GENRE_ALIAS[String(raw).toLowerCase().trim()] || raw;
// Broad umbrellas a genre belongs to, so IGDB "Platform" and sheet "3D Platformer"
// both file under "Platformer". Only the four families you flagged.
function genreUmbrellas(v) {
  const s = String(v).toLowerCase(), out = [];
  if (/platform/.test(s)) out.push("Platformer");
  if (/\brpg\b|role-playing|mmorpg/.test(s)) out.push("RPG");
  if (/shooter/.test(s)) out.push("Shooter");
  if (/\bstrateg/.test(s)) out.push("Strategy");
  return out;
}

// Sheet spelling wins as the canonical label. Built once per dataset from BOTH sheets.
let _vocab = null, _vocabFor = null;
function unifyVocab() {
  if (_vocabFor === DATA && _vocab) return _vocab;
  const dev = {}, pub = {}, fran = {};
  const put = (map, val, norm) => { const k = norm(val); if (k && !(k in map)) map[k] = String(val); };
  for (const key of ["games", "completed"]) {
    for (const r of ((DATA.sheets[key] || {}).rows || [])) {
      if (r.developer) put(dev, r.developer, normCompany);
      if (r.publisher) put(pub, r.publisher, normCompany);
      if (r.franchise) put(fran, r.franchise, normFranchise);
    }
  }
  _vocab = { dev, pub, fran }; _vocabFor = DATA;
  return _vocab;
}

function unifiedGenreVals(row) {
  const out = new Set();
  const add = (raw) => {
    if (raw == null || raw === "") return;
    const c = canonGenre(raw);
    out.add(String(c));
    genreUmbrellas(c).forEach((u) => out.add(u));
    genreUmbrellas(raw).forEach((u) => out.add(u));
  };
  if (row.genre) add(row.genre);
  const e = ENRICH[row._k];
  if (e && e.genres) for (const g of e.genres) add(g);
  return [...out];
}
function unifiedCompanyVals(sheetVal, igdbArr, map) {
  const out = new Set();
  const add = (n) => { if (n) out.add(map[normCompany(n)] || String(n)); };
  add(sheetVal);
  (igdbArr || []).forEach(add);
  return [...out];
}
function unifiedDevVals(row) {
  return unifiedCompanyVals(row.developer, (ENRICH[row._k] || {}).developers, unifyVocab().dev);
}
function unifiedPubVals(row) {
  return unifiedCompanyVals(row.publisher, (ENRICH[row._k] || {}).publishers, unifyVocab().pub);
}
function unifiedFranchiseVals(row) {
  const out = new Set(), map = unifyVocab().fran;
  const add = (n) => { if (n) out.add(map[normFranchise(n)] || String(n)); };
  add(row.franchise);
  const e = ENRICH[row._k];
  if (e && e.franchises) for (const f of e.franchises) add(f);
  return [...out];
}
const UNIFIED_GETVALS = {
  genre: unifiedGenreVals, developer: unifiedDevVals,
  publisher: unifiedPubVals, franchise: unifiedFranchiseVals,
};
// The canonical (sheet-spelling) form of an external value — so a drawer link filters
// the value that's actually IN the facet, not the raw IGDB string.
const canonDev = (n) => unifyVocab().dev[normCompany(n)] || String(n);
const canonPub = (n) => unifyVocab().pub[normCompany(n)] || String(n);
const canonFran = (n) => unifyVocab().fran[normFranchise(n)] || String(n);
// Turn a scalar sheet facet column into the joined multi-valued one, keeping its key
// and label (so drawer facet-links and URL state keep working unchanged).
const unifiedFacetCol = (c) =>
  (ENRICH_ENABLED && UNIFIED_GETVALS[c.key]) ? { ...c, kind: "fn", getVals: UNIFIED_GETVALS[c.key] } : c;
const PLAYTIME_BUCKETS = [
  { label: "< 2h", test: (h) => h < 2 },
  { label: "2–5h", test: (h) => h >= 2 && h < 5 },
  { label: "5–10h", test: (h) => h >= 5 && h < 10 },
  { label: "10–20h", test: (h) => h >= 10 && h < 20 },
  { label: "20–40h", test: (h) => h >= 20 && h < 40 },
  { label: "40–80h", test: (h) => h >= 40 && h < 80 },
  { label: "80h+", test: (h) => h >= 80 },
];
const SALES_BUCKETS = [
  { label: "10m+", test: (v) => v >= 10e6 },
  { label: "5–10m", test: (v) => v >= 5e6 && v < 10e6 },
  { label: "1–5m", test: (v) => v >= 1e6 && v < 5e6 },
  { label: "500k–1m", test: (v) => v >= 5e5 && v < 1e6 },
  { label: "< 500k", test: (v) => v < 5e5 },
];
const METACRITIC_BUCKETS = [
  { label: "90–100", test: (v) => v >= 0.9 },
  { label: "80–89", test: (v) => v >= 0.8 && v < 0.9 },
  { label: "70–79", test: (v) => v >= 0.7 && v < 0.8 },
  { label: "60–69", test: (v) => v >= 0.6 && v < 0.7 },
  { label: "< 60", test: (v) => v < 0.6 },
];
// Best playtime for a row: HLTB (main→best) where enriched, else sheet estimate.
const playtimeOf = (row) => {
  const e = ENRICH[row._k];
  if (e && e.hltbBest != null) return e.hltbBest;
  if (e && e.vnHours != null) return e.vnHours;   // HLTB barely covers VNs
  return row.estimatedTime;
};
// Metacritic (0–1): scraped score where enriched, else the sheet's Metacritic Rating.
const metacriticOf = (row) => { const e = ENRICH[row._k]; return e && e.metascore != null ? e.metascore / 100 : row.metacriticRating; };
// User rating (0–1): IGDB community rating where enriched, else VNDB's (visual
// novels are the one place VNDB's vote count dwarfs everyone's), else GameFAQs.
const userRatingOf = (row) => {
  const e = ENRICH[row._k];
  if (e && e.userRating != null) return e.userRating;
  if (e && e.vnRating != null) return e.vnRating;
  return row.gamefaqsUserRating;
};
// ---- launching a game ----------------------------------------------------
// IGDB's external_games gives us storefront ids; the sheet's Notes column says
// WHICH storefront the copy you own came from. So Notes picks the target and
// IGDB supplies the id — a game on Steam, GOG and Epic launches on the one you
// actually bought it on.
//
// Only a few storefronts expose a real launch URI. The rest get an "open" link,
// which on the right device does the next best thing: the App Store link opens
// the App Store app (with its own Open button), the Microsoft Store link opens
// the Store app, and so on. Pretending we can launch those would just look
// broken, so we don't claim to.
const STORE_LAUNCH = {
  steam: { label: "Play on Steam", uri: (id) => `steam://rungameid/${id}` },
  gog: { label: "Play on GOG", uri: (id) => `goggalaxy://openGameView/${id}` },
  // Best-effort: the Amazon Games launcher registers amazon-games:// and the
  // IGDB uid is the same amzn1.adg.product.* id it uses.
  amazon: { label: "Play on Amazon", uri: (id) => `amazon-games://play/${id}` },
};
const STORE_OPEN = {
  epic: "Epic Games Store", itch: "itch.io", appstore: "App Store",
  googleplay: "Google Play", xbox: "Microsoft Store", microsoft: "Microsoft Store",
  playstation: "PlayStation Store", steam: "Steam", gog: "GOG", amazon: "Amazon",
};

// The sheet's Notes vocabulary → an IGDB storefront. Only the ones IGDB can
// actually give us an id for: Origin/EA, Ubisoft Connect and Battle.net have no
// IGDB source at all, so there's no id to launch with and we say nothing rather
// than offering a button that can't work.
const NOTES_STORE = {
  "Steam": "steam", "GOG": "gog", "Epic Games Store": "epic", "itch.io": "itch",
  "Amazon": "amazon", "Xbox Game Pass": "xbox", "Microsoft Store": "microsoft",
};
// Failing that, the platform implies a storefront.
const PLATFORM_STORE = {
  "iOS": "appstore", "32-bit iOS": "appstore", "Android": "googleplay",
  "Xbox One": "xbox", "Xbox Series X|S": "xbox", "Xbox 360": "xbox",
  "PlayStation 4": "playstation", "PlayStation 5": "playstation",
  "PlayStation 3": "playstation", "PC": "steam",
};

const storeEntry = (e, key) => {
  const st = (e && e.stores && e.stores[key]) || null;
  if (!st) return null;
  return typeof st === "string" ? { id: st, url: null } : st;   // tolerate the old shape
};

function launchTarget(row) {
  const e = ENRICH[row._k];
  if (!e || !e.stores) return null;
  const notes = String(row.notes || "");
  const want = NOTES_STORE[notes] || PLATFORM_STORE[row.platform] || null;

  // The storefront the sheet says you own it on — else whatever we know about.
  const key = (want && storeEntry(e, want)) ? want
    : Object.keys(STORE_OPEN).find((k) => storeEntry(e, k));
  if (!key) return null;
  const st = storeEntry(e, key);
  const store = STORE_OPEN[key] || key;

  // A launch only makes sense when the sheet says this is the copy you own.
  const ownThisOne = row.owned && want === key && !!STORE_LAUNCH[key];
  if (ownThisOne) {
    return { kind: "launch", label: "▶ " + STORE_LAUNCH[key].label,
             href: STORE_LAUNCH[key].uri(st.id), store };
  }
  const url = st.url || storeUrl(key, st.id);
  if (!url) return null;
  return { kind: "store", label: `${row.owned && want === key ? "Open in" : "View on"} ${store}`,
           href: url, store };
}

// A few sources give an id but no url, and a couple of schemes open the native
// store app rather than a web page, which is closer to "launch" than a link.
function storeUrl(key, id) {
  switch (key) {
    case "steam": return `https://store.steampowered.com/app/${id}/`;
    case "appstore": return `https://apps.apple.com/app/id${id}`;
    case "googleplay": return `https://play.google.com/store/apps/details?id=${id}`;
    case "xbox": case "microsoft": return `ms-windows-store://pdp/?ProductId=${id}`;
    case "playstation": return `https://store.playstation.com/concept/${id}`;
    default: return null;
  }
}

/* ---- RomM: play it in the browser --------------------------------------
   Joined on (IGDB game id, platform) — an id join on both axes, not a title
   match. The catch is that the two systems name the same machine differently:
   the sheet says "PlayStation", the NAS folder is "PSX". 27 of the 45 playable
   platforms are spelled identically; these are the rest.

   "PC" -> "MS-DOS" is the interesting one. A PC row only lights up if the SAME
   IGDB game also exists in the DOS folder — so Doom gets a Play button and a
   modern Steam game simply doesn't match. The id join makes that safe. */
const ROMM_PLATFORM = {
  "PlayStation": "PSX",
  "PlayStation Portable": "PSP",
  "Sega Genesis": "Genesis",
  "Sega Saturn": "Saturn",
  "Sega Master System": "Master System",
  "Sega Game Gear": "Game Gear",
  "Commodore Amiga": "Amiga",
  "Commodore Amiga CD32": "Amiga CD32",
  "Commodore VIC-20": "VIC-20",
  "Commodore Plus/4": "Commodore Plus-4",
  "Philips CD-i": "CD-i",
  "Atari Jaguar": "Jaguar",
  "Atari Lynx": "Lynx",
  "Neo-Geo Pocket": "Neo Geo Pocket",
  "Neo-Geo Pocket Color": "Neo Geo Pocket Color",
  "Arcade": "MAME",
  "PC": "MS-DOS",
};

let ROMM = { enabled: false, baseUrl: "", roms: {} };
async function loadRomm() {
  try {
    const r = await fetch("/api/romm");
    if (!r.ok) return;
    ROMM = await r.json();
    if (ROMM.enabled) patchPlayButtons();
  } catch (_) { /* RomM being down must never break gamedex */ }
}

// The rom id for this row, or null. Requires BOTH the game and the platform to
// agree — a PSX Doom must not offer to play the 3DO one.
function rommRomId(row) {
  if (!ROMM.enabled || !row) return null;
  const e = ENRICH[row._k];
  if (!e || !e.igdbId) return null;
  const folder = ROMM_PLATFORM[row.platform] || row.platform;
  const id = ROMM.roms[`${e.igdbId}|${folder}`];
  return id != null ? id : null;
}

const rommPlayUrl = (id) => `${ROMM.baseUrl}/console/rom/${id}/play`;

function rommHtml(row) {
  const id = rommRomId(row);
  if (!id) return "";
  return `<a class="btn play" href="${escapeHtml(rommPlayUrl(id))}" target="_blank" rel="noopener"
     title="Play in the browser via RomM">${icon("i-play", 15)} Play now</a>`;
}

// The map arrives after the drawer may already be open; fill it in rather than
// re-render (the same trap the enrichment map set five times over).
function patchPlayButtons() {
  const body = $("#drawerBody");
  if (!body || !drawerRow) return;
  if (body.querySelector(".btn.play")) return;          // already there
  const html = rommHtml(drawerRow);
  if (!html) return;
  let host = body.querySelector(".hero-actions");
  if (host) { host.insertAdjacentHTML("afterbegin", html); return; }
  // A game with no storefront at all has no actions row yet — give it one.
  const hero = body.querySelector(".hero") || body.firstElementChild;
  if (hero) hero.insertAdjacentHTML("beforeend", `<div class="hero-actions">${html}</div>`);
}

function launchHtml(row) {
  const t = launchTarget(row);
  if (!t) return rommHtml(row);
  const external = /^https?:/.test(t.href);
  const store = t.kind === "launch"
    ? `<a class="btn launch" href="${escapeHtml(t.href)}">${escapeHtml(t.label)}</a>`
    : `<a class="btn ghost" href="${escapeHtml(t.href)}"${external ? ' target="_blank" rel="noopener"' : ""}>${escapeHtml(t.label)}${external ? " ↗" : ""}</a>`;
  return rommHtml(row) + store;      // playing it beats buying it
}

// Units sold/shipped (VGChartz estimate). Only major releases have a figure.
const salesOf = (row) => { const e = ENRICH[row._k]; return e && e.units != null ? e.units : null; };
const fmtUnits = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + "m"
  : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));

// Quantity of copies owned, parsed from the notes ("Two copies owned" → 2).
const _NUMWORD = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8 };
function quantityFromNotes(notes) {
  if (!notes) return 1;
  const s = String(notes);
  let m = s.match(/(\d+)\s+cop(?:y|ies)/i);
  if (m) return parseInt(m[1], 10);
  m = s.match(/\b(one|two|three|four|five|six|seven|eight)\s+cop(?:y|ies)/i);
  return m ? _NUMWORD[m[1].toLowerCase()] || 1 : 1;
}
// Map the sheet's Condition to a GameEye price key.
const _COND_KEY = { complete: "geCib", cib: "geCib", loose: "geLoose", new: "geNew" };
// Collection value for an owned row: GameEye price for its condition × quantity.
const r0 = (v) => typeof v === "number" && v > 0;   // a real price, not free/blank

function collectionValueOf(row) {
  const e = ENRICH[row._k];
  if (!e) return null;
  const price = e[_COND_KEY[(row.condition || "").toLowerCase()] || "geLoose"];
  return price != null ? price * quantityFromNotes(row.notes) : null;
}
function bucketLabel(v, buckets) { for (const b of buckets) if (b.test(v)) return b.label; return null; }

// What each source found for a row (used by the metadata facets).
const metaOf = (row) => {
  const e = ENRICH[row._k] || null;
  return {
    igdb: !!(e && e.igdbId),                        // IGDB proper
    fallback: e && e.source ? e.source : null,      // IGN / Steam / LaunchBox
    hltb: !!(e && e.hltbBest != null),
    mc: !!(e && e.metascore != null),
    art: !!(e && (e.coverUrl || e.cover || e.vnCover || e.adbCover)),
  };
};
// Which of the extra sources have data for this row (multi-valued).
function extraSourcesOf(row) {
  const e = ENRICH[row._k];
  if (!e) return [];
  const out = [];
  if (e.adbUrl) out.push("Arcade Database");
  if (e.vnUrl) out.push("VNDB");
  if (e.units != null) out.push("VGChartz sales");
  if (e.thumbyUrl) out.push("Thumby");
  if (e.deck || e.protonTier) out.push("Steam extras");
  if (e.wrTime) out.push("speedrun.com");
  if (e.guideUrl) out.push("StrategyWiki");
  if (e.hltbUrl) out.push("HowLongToBeat");
  if (e.metaUrl) out.push("Metacritic");
  if (e.geUrl) out.push("GameEye");
  return out;
}

// Which source supplied the game's primary metadata.
function metaSourceOf(row) {
  const m = metaOf(row);
  return m.igdb ? "IGDB" : m.fallback || "None";
}
// Tags for what a row is MISSING — multi-valued, so one game can carry several.
function missingOf(row) {
  const m = metaOf(row);
  const out = [];
  if (!m.igdb) out.push("No IGDB");
  if (!m.art) out.push("No cover / art");
  if (!m.hltb) out.push("No HLTB");
  if (!m.mc) out.push("No Metacritic");
  if (!m.igdb && !m.fallback && !m.art && !m.hltb && !m.mc) out.push("Nothing at all");
  return out;
}

const igdbFacetCols = () =>
  ENRICH_ENABLED
    ? [
        ...IGDB_FACET_DEFS.map((d) => ({ ...d, type: "text", facet: true, virtual: true })),
        { key: "__meta_src", label: "Metadata source", type: "text", facet: true, virtual: true, kind: "fn", getVals: (r) => [metaSourceOf(r)] },
        { key: "__extra_src", label: "Enriched by", type: "text", facet: true, virtual: true, kind: "fn", getVals: extraSourcesOf },
        { key: "__missing", label: "Missing data", type: "text", facet: true, virtual: true, kind: "fn", getVals: missingOf },
      ]
    : [];
// Bucketed facets available on the Games tab (playtime + Metacritic).
function extraFacetCols(tab = activeTab) {
  if (tab !== "games") return [];
  return [
    { key: "__playtime", label: "Playtime", type: "text", facet: true, virtual: true, kind: "bucket", buckets: PLAYTIME_BUCKETS, getVal: playtimeOf },
    { key: "__metacritic", label: "Metacritic", type: "text", facet: true, virtual: true, kind: "bucket", buckets: METACRITIC_BUCKETS, getVal: metacriticOf },
    { key: "__userrating", label: "User Rating", type: "text", facet: true, virtual: true, kind: "bucket", buckets: METACRITIC_BUCKETS, getVal: userRatingOf },
    { key: "__sales", label: "Sales (VGChartz)", type: "text", facet: true, virtual: true, kind: "bucket", buckets: SALES_BUCKETS, getVal: salesOf },
    // Arcade-only, from the MAME romset lookup — blank for everything else.
    { key: "__adbplayers", label: "Arcade players", type: "text", facet: true, virtual: true, kind: "fn",
      getVals: (r) => { const e = ENRICH[r._k]; return e && e.adbPlayers ? [e.adbPlayers] : []; } },
    { key: "__adborient", label: "Arcade screen", type: "text", facet: true, virtual: true, kind: "fn",
      getVals: (r) => { const e = ENRICH[r._k]; return e && e.adbOrientation ? [e.adbOrientation] : []; } },
    // You track Steam Deck completions in the sheet — now you can filter the
    // backlog down to what Valve says actually runs on it.
    { key: "__deck", label: "Steam Deck", type: "text", facet: true, virtual: true, kind: "fn",
      getVals: (r) => { const e = ENRICH[r._k]; return e && e.deck ? [e.deck] : []; } },
    { key: "__proton", label: "ProtonDB", type: "text", facet: true, virtual: true, kind: "fn",
      // The API returns "platinum"; it's a tier, not a word in a sentence.
      getVals: (r) => { const e = ENRICH[r._k]; return e && e.protonTier ? [titleCase(e.protonTier)] : []; } },
    { key: "__steamrev", label: "Steam reviews", type: "text", facet: true, virtual: true, kind: "bucket",
      buckets: METACRITIC_BUCKETS, getVal: (r) => { const e = ENRICH[r._k]; return e && e.steamReview; } },
    // What we think you'd score it — a filter for "things I'd probably love".
    { key: "__predicted", label: "Predicted for you", type: "text", facet: true, virtual: true, kind: "bucket",
      buckets: METACRITIC_BUCKETS, getVal: predictedOf },
  ];
}
const facetCols = () => [...columns().filter((c) => c.facet).map(unifiedFacetCol), ...igdbFacetCols(), ...extraFacetCols()];
const facetColByKey = (key) => facetCols().find((c) => c.key === key);

// A row's facet values as [{key, raw}] — scalar → one, arrays → many, bucket → one label.
function rowFacetItems(row, col) {
  if (col.kind === "fn") {                    // computed, possibly multi-valued
    return (col.getVals(row) || []).map((x) => ({ key: String(x), raw: x }));
  }
  if (col.kind === "bucket") {
    const v = col.getVal(row);
    if (v === undefined || v === null || v === "") return [];
    const lbl = bucketLabel(Number(v), col.buckets);
    return lbl ? [{ key: lbl, raw: lbl }] : [];
  }
  let v;
  if (col.virtual) { const e = ENRICH[row._k]; v = e ? e[col.source] : undefined; }
  else v = row[col.key];
  if (v === undefined || v === null || v === "") return [];
  const arr = Array.isArray(v) ? v : [v];
  return arr.filter((x) => x !== undefined && x !== null && x !== "").map((x) => ({ key: String(x), raw: x }));
}

function facetLabel(col, value) {
  if (col.type === "bool") return value ? "Yes" : "No";
  return String(value);
}

// ---- filtering ----------------------------------------------------------
// Row matches free-text search.
// The searchable text of a row, built once and kept. A WeakMap keyed on the row
// object means a fresh spreadsheet invalidates it for free.
const HAYSTACK = new WeakMap();
function rowHaystack(row, cols) {
  let hay = HAYSTACK.get(row);
  if (hay === undefined) {
    hay = cols.map((k) => row[k]).filter((v) => v != null).join(" ").toLowerCase();
    HAYSTACK.set(row, hay);
  }
  return hay;
}
// IGDB genres are searchable too — so "Platformer" finds games IGDB tagged Platform,
// not only the ones your sheet spells that way. Kept separate from rowHaystack (which is
// sheet-only and immutable) and re-derived when enrichment lands, since ENRICH fills in
// after the first paint. Cheap: unmatched rows return "" without touching the vocab.
let _enrichEpoch = 0;
const _genreHay = new WeakMap();
function searchGenreHay(row) {
  if (!ENRICH_ENABLED || !ENRICH[row._k]) return "";
  const c = _genreHay.get(row);
  if (c && c.e === _enrichEpoch) return c.h;
  const h = unifiedGenreVals(row).join(" ").toLowerCase();
  _genreHay.set(row, { e: _enrichEpoch, h });
  return h;
}
function matchesSearch(row, terms, cols) {
  if (!terms.length) return true;
  const hay = rowHaystack(row, cols);
  return terms.every((t) => hay.includes(t) || searchGenreHay(row).includes(t));
}
// Row matches a facet selection (Set of value keys). OR within a facet; for
// IGDB array facets a row matches if ANY of its values is selected.
function matchesFacet(row, col, selected) {
  if (!selected || selected.size === 0) return true;
  return rowFacetItems(row, col).some((it) => selected.has(it.key));
}

// The search half of the filter, memoised. renderFacets() calls filterRows once
// per facet column (20+ times) and the search term is the same for every one of
// them, so scanning the sheet each time was pure waste.
let _searchBase = { tab: null, q: null, rows: null };
function searchedRows() {
  const st = tabState[activeTab];
  if (_searchBase.tab === activeTab && _searchBase.q === st.search) return _searchBase.rows;
  const terms = st.search.toLowerCase().split(/\s+/).filter(Boolean);
  const sCols = searchCols();
  const rows = terms.length
    ? sheet().rows.filter((row) => matchesSearch(row, terms, sCols))
    : sheet().rows;
  _searchBase = { tab: activeTab, q: st.search, rows };
  return rows;
}
const resetSearchCache = () => { _searchBase = { tab: null, q: null, rows: null }; };

// Rows matching search + every facet EXCEPT `skipKey` (for facet counts) or all.
// Callers never mutate the result (sortRows copies), so the no-facet case can
// hand back the memoised array as-is.
function filterRows(skipKey) {
  const st = tabState[activeTab];
  const active = Object.keys(st.facets)
    .map((k) => [facetColByKey(k), st.facets[k]])
    .filter(([c]) => c && c.key !== skipKey && st.facets[c.key] && st.facets[c.key].size);
  const base = searchedRows();
  if (!active.length) return base;
  return base.filter((row) => {
    for (const [col, sel] of active) {
      if (!matchesFacet(row, col, sel)) return false;
    }
    return true;
  });
}

// ---- rendering: facets --------------------------------------------------
function setFacets(open) {
  $("#facets").classList.toggle("open", open);
  $("#facetBackdrop").hidden = !open;
  syncScrollLock();
}

function renderFacets() {
  const st = tabState[activeTab];
  const host = $("#facets");
  host.innerHTML = "";

  const closeBtn = document.createElement("button");   // mobile-only (CSS)
  closeBtn.className = "facet-close";
  closeBtn.textContent = "✕ Close filters";
  closeBtn.onclick = () => setFacets(false);
  host.appendChild(closeBtn);

  for (const col of facetCols()) {
    // Count values across rows filtered by the OTHER facets + search.
    const base = filterRows(col.key);
    const counts = new Map();
    for (const row of base) {
      for (const it of rowFacetItems(row, col)) {
        counts.set(it.key, (counts.get(it.key) || 0) + 1);
      }
    }
    const selected = st.facets[col.key] || new Set();
    // Always include selected values even if their current count is 0.
    for (const s of selected) if (!counts.has(s)) counts.set(s, 0);
    if (counts.size === 0) continue;

    let values = [...counts.entries()].map(([k, n]) => ({
      key: k,
      label: facetLabel(col, col.type === "bool" ? k === "true" : k),
      count: n,
    }));
    if (col.buckets) {                                   // fixed bucket order
      const ord = new Map(col.buckets.map((b, i) => [b.label, i]));
      values.sort((a, b) => (ord.get(a.key) ?? 99) - (ord.get(b.key) ?? 99));
    } else {
      const numeric = col.type === "year" || col.type === "int" || col.type === "number";
      // For year facets, non-numeric labels (e.g. "Early Access") sort as newest.
      const nkey = (k) => { const n = Number(k); return isNaN(n) ? Infinity : n; };
      values.sort((a, b) =>
        numeric ? nkey(b.key) - nkey(a.key) : b.count - a.count || a.label.localeCompare(b.label)
      );
    }

    const group = document.createElement("div");
    group.className = "facet" + (st.expanded[col.key] === false ? " collapsed" : "");

    const head = document.createElement("div");
    head.className = "facet-head";
    const nSel = selected.size ? ` (${selected.size})` : "";
    head.innerHTML = `<span>${escapeHtml(col.label)}${nSel}</span><span class="chev">▼</span>`;
    head.onclick = () => {
      st.expanded[col.key] = st.expanded[col.key] === false;
      renderFacets();
    };
    group.appendChild(head);

    const body = document.createElement("div");
    body.className = "facet-body";

    const filterKey = "__f_" + col.key;
    const showAll = st.expanded[filterKey + "_all"];
    let filterText = st.expanded[filterKey] || "";
    // The options list is rebuilt on its own when you type in the filter box —
    // never via renderFacets(). Re-rendering the whole sidebar recomputed the
    // counts for every column (the expensive part), and worse, it destroyed the
    // input you were typing into: the old code then "restored focus" by querying
    // the DETACHED group element, which finds the old input and focuses nothing.
    // Keeping the input alive means focus and caret survive for free.
    const optionsBox = document.createElement("div");
    optionsBox.className = "facet-options";

    const paintOptions = () => {
      optionsBox.innerHTML = "";
      const q = (st.expanded[filterKey] || "").toLowerCase();
      const shown = q ? values.filter((v) => v.label.toLowerCase().includes(q)) : values;
      const seeAll = st.expanded[filterKey + "_all"];
      const capped = !seeAll && !q && shown.length > FACET_CAP;
      const visible = capped ? shown.slice(0, FACET_CAP) : shown;

      for (const v of visible) {
        const opt = document.createElement("label");
        const isChecked = selected.has(v.key);
        opt.className = "facet-opt" + (isChecked ? " checked" : "");
        opt.innerHTML =
          `<input type="checkbox" ${isChecked ? "checked" : ""}/>` +
          `<span class="lbl" title="${escapeHtml(v.label)}">${escapeHtml(v.label)}</span>` +
          `<span class="cnt">${v.count.toLocaleString()}</span>`;
        opt.querySelector("input").onchange = () => {
          const set = st.facets[col.key] || new Set();
          if (set.has(v.key)) set.delete(v.key);
          else set.add(v.key);
          if (set.size) st.facets[col.key] = set;
          else delete st.facets[col.key];
          st.page = 1;
          renderAll();
          nav();
        };
        optionsBox.appendChild(opt);
      }

      if (!visible.length) {
        const none = document.createElement("div");
        none.className = "facet-none";
        none.textContent = "No matches";
        optionsBox.appendChild(none);
      }
      if (capped) {
        const more = document.createElement("button");
        more.className = "facet-more";
        more.textContent = `Show ${shown.length - FACET_CAP} more…`;
        more.onclick = () => { st.expanded[filterKey + "_all"] = true; paintOptions(); };
        optionsBox.appendChild(more);
      } else if (seeAll && shown.length > FACET_CAP && !q) {
        const less = document.createElement("button");
        less.className = "facet-more";
        less.textContent = "Show less";
        less.onclick = () => { st.expanded[filterKey + "_all"] = false; paintOptions(); };
        optionsBox.appendChild(less);
      }
    };

    if (values.length > FACET_FILTER_THRESHOLD) {
      const fi = document.createElement("input");
      fi.className = "facet-filter";
      fi.type = "search";
      fi.placeholder = `Filter ${col.label.toLowerCase()}…`;
      fi.value = filterText;
      fi.oninput = () => {
        st.expanded[filterKey] = fi.value;
        paintOptions();          // this group only; the input is never replaced
      };
      // Same field as every other search box: icon inside, one focus ring.
      const fwrap = document.createElement("span");
      fwrap.className = "field field-facet";
      fwrap.innerHTML = `<svg class="ico" width="13" height="13" aria-hidden="true"><use href="#i-search"/></svg>`;
      fwrap.appendChild(fi);
      body.appendChild(fwrap);
    }

    paintOptions();
    body.appendChild(optionsBox);

    group.appendChild(body);
    host.appendChild(group);
  }
}

// ---- rendering: table ---------------------------------------------------
// ---- multi-key sorting --------------------------------------------------
const NUMERIC_TYPES = ["rating", "hours", "number", "money", "int", "year"];

// Per-tab default sort. A spec is {key, dir, type?, kind?}; `kind` selects a
// custom comparator. The games default: Playing-status group on top
// (Playing→On Hold→Up Next→none), then uncompleted before completed, then
// newest release year, with newest release date (Early Access = newest) as the
// final tiebreaker.
const DEFAULT_SORT = {
  games: [{ key: "releaseDate", kind: "releaseDateDesc", dir: "desc" }],
  completed: [{ key: "date", dir: "desc", type: "date" }],
  onOrder: [{ key: "orderedDate", dir: "desc", type: "date" }],
};

const PLAYING_RANK = { "Playing": 0, "On Hold": 1, "Up Next": 2 };
const isBlank = (v) => v === undefined || v === null || v === "";

function playingRank(v) {
  if (isBlank(v)) return 3;
  if (v in PLAYING_RANK) return PLAYING_RANK[v];
  const n = Number(v);                 // tolerate raw codes 1/0/-1 too
  return n === 1 ? 0 : n === 0 ? 1 : n === -1 ? 2 : 3;
}
function releaseDateScore(v) {
  if (isBlank(v)) return -Infinity;                        // no date → oldest
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return new Date(v + "T00:00:00").getTime();
  return Infinity;                                          // Early Access/TBD → newest
}
function cmpBy(a, b, spec) {
  const v = spec.kind && VIRTUAL_SORTS.find((s) => s.kind === spec.kind);
  const x = v ? v.get(a) : a[spec.key];
  const y = v ? v.get(b) : b[spec.key];
  if (spec.kind === "playingRank") return playingRank(x) - playingRank(y);
  if (spec.key === "priority") {
    // Alphabetically, "Might Play" beats "Must Play" beats "Want to Play". Rank it.
    const d = priorityRank(x) - priorityRank(y);
    return spec.dir === "desc" ? -d : d;
  }
  if (spec.kind === "releaseDateDesc") return releaseDateScore(y) - releaseDateScore(x);
  const xm = isBlank(x), ym = isBlank(y);
  if (xm && ym) return 0;
  if (xm) return 1;   // blanks always sink, regardless of direction
  if (ym) return -1;
  const dir = spec.dir === "desc" ? -1 : 1;
  const type = spec.type || (colByKey(spec.key) || {}).type;
  if (NUMERIC_TYPES.includes(type)) {
    const nx = Number(x), ny = Number(y);          // "Early Access" (NaN) = newest
    return ((isNaN(nx) ? Infinity : nx) - (isNaN(ny) ? Infinity : ny)) * dir;
  }
  if (type === "bool") return ((x ? 1 : 0) - (y ? 1 : 0)) * dir;
  return String(x).localeCompare(String(y), undefined, { sensitivity: "base" }) * dir;
}
function effectiveSort() {
  const st = tabState[activeTab];
  if (st.sort && st.sort.length) return st.sort;
  return DEFAULT_SORT[activeTab] ||
    [{ key: (columns().find((c) => c.primary) || columns()[0]).key, dir: "asc" }];
}
// Naive search relevance: a query that hits the TITLE outranks one that only hit
// another field (genre, publisher…). So searching "Adventure" surfaces the game
// *named* Adventure above everything merely tagged with the Adventure genre.
function searchRank(row, terms) {
  const title = String(row.title ?? row.game ?? "").toLowerCase();
  let score = 0;
  for (const t of terms) {
    if (title === t) score += 100;
    else if (title.startsWith(t)) score += 40;
    else if (title.includes(t)) score += 20;
    // matched only via another field (it passed the filter) — no title bonus
  }
  return score;
}

function sortRows(rows) {
  const spec = effectiveSort();
  const q = (tabState[activeTab].search || "").toLowerCase().trim();
  const terms = q ? q.split(/\s+/).filter(Boolean) : [];
  return [...rows].sort((a, b) => {
    if (terms.length) {
      const r = searchRank(b, terms) - searchRank(a, terms);
      if (r) return r;                    // title relevance first when searching
    }
    for (const s of spec) { const c = cmpBy(a, b, s); if (c) return c; }
    return 0;
  });
}

// Click a header to sort by it (toggles dir); Shift-click to add/toggle it as
// an additional sort level (or remove it on a third shift-click).
function onHeaderClick(col, shift) {
  const st = tabState[activeTab];
  const cur = st.sort && st.sort.length ? st.sort.slice() : [];
  const idx = cur.findIndex((s) => s.key === col.key);
  const defDir = col.type === "text" ? "asc" : "desc";
  if (shift) {
    if (idx === -1) cur.push({ key: col.key, dir: defDir, type: col.type });
    else if (cur[idx].dir === defDir) cur[idx] = { key: col.key, dir: defDir === "asc" ? "desc" : "asc", type: col.type };
    else cur.splice(idx, 1);                       // third shift-click drops this level
  } else {
    if (cur.length === 1 && cur[0].key === col.key)
      cur.splice(0, 1, { key: col.key, dir: cur[0].dir === "asc" ? "desc" : "asc", type: col.type });
    else { cur.length = 0; cur.push({ key: col.key, dir: defDir, type: col.type }); }
  }
  st.sort = cur.length ? cur : null;
  st.page = 1;
  renderAll();
  nav();
}

// Dispatcher: sort → paginate → render as table or grid.
function renderTable(rows) {
  const st = tabState[activeTab];
  // Combining: rows sharing an IGDB id ARE the same game, so collapse them before
  // sorting and paging — otherwise the counts and page numbers would lie.
  const base = st.combine ? groupByGame(rows) : rows;
  const sorted = sortRows(base);
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  if (st.page > pages) st.page = pages;
  const start = (st.page - 1) * PAGE_SIZE;
  const pageRows = sorted.slice(start, start + PAGE_SIZE);

  // The timeline is a view of the Completed rows — same filters, same search.
  const canTimeline = activeTab === "completed";
  $("#viewTimeline").hidden = !canTimeline;
  if (st.view === "timeline" && !canTimeline) st.view = "grid";
  const view = st.view;
  $("#tablewrap").hidden = view !== "table";
  $("#gridwrap").hidden = view !== "grid";
  $("#timeline").hidden = view !== "timeline";
  $("#pager").style.display = view === "timeline" ? "none" : "";
  for (const [id, m] of [["viewTable", "table"], ["viewGrid", "grid"], ["viewTimeline", "timeline"]]) {
    $("#" + id).classList.toggle("active", view === m);
  }
  // Combining is meaningless on the timeline, which plots completions by date.
  $("#combine").hidden = view === "timeline";
  $("#combine").classList.toggle("active", st.combine);
  $("#combine").setAttribute("aria-pressed", String(!!st.combine));
  if (view === "timeline") {
    renderTimeline(rows);
    $("#count").textContent = `${rows.length.toLocaleString()} of ${sheet().rows.length.toLocaleString()} games`;
    renderViews();
    return;
  }
  $("#gridsortwrap").hidden = false;    // sort control in both views (reaches
  populateGridSort();                   // non-primary columns like Date Added)
  if (!sorted.length) {
    const filtered = st.search || Object.keys(st.facets).length;
    const host = view === "grid" ? $("#grid") : $("#tbody");
    host.innerHTML = view === "grid"
      ? emptyState("No games match", filtered ? "Try loosening a filter or clearing the search." : "Nothing here yet.", filtered ? "Clear filters" : null)
      : `<tr><td colspan="99">${emptyState("No games match", "Try loosening a filter.", null)}</td></tr>`;
    if (view === "grid") $("#thead").innerHTML = "";
    const act = $("#emptyAction");
    if (act) act.onclick = () => { st.search = ""; st.facets = {}; st.page = 1; $("#search").value = ""; renderAll(); nav(); };
  } else if (view === "grid") renderGrid(pageRows);
  else renderTableView(pageRows);

  maybeEnrich(pageRows);
  kbReset();
  renderViews();
  $("#count").textContent = `${sorted.length.toLocaleString()} of ${sheet().rows.length.toLocaleString()} games`;
  $("#clear").hidden = !(st.search || Object.keys(st.facets).length);
  $("#resetsort").hidden = !(st.sort && st.sort.length);
  renderPager(pages);
}

function renderTableView(pageRows) {
  const cols = columns().filter((c) => c.primary);
  const spec = effectiveSort();
  // Surface sorted-by columns that aren't shown (e.g. Date Added) as extra columns.
  for (const s of spec) {
    const c = colByKey(s.key);
    if (c && !cols.includes(c)) cols.push(c);
  }
  const thead = $("#thead");
  thead.innerHTML = "";
  if (ENRICH_ENABLED) thead.appendChild(document.createElement("th")).className = "cover-h";
  const specByKey = new Map(spec.map((s, i) => [s.key, { dir: s.dir, ord: i }]));
  const multi = spec.length > 1;
  for (const c of cols) {
    const th = document.createElement("th");
    const s = specByKey.get(c.key);
    let ind = "";
    if (s) {
      const glyph = s.dir === "asc" ? "▲" : "▼";
      ind = `<span class="arrow">${glyph}${multi ? `<sub>${s.ord + 1}</sub>` : ""}</span>`;
    }
    th.innerHTML = `${escapeHtml(c.label)} ${ind}`;
    th.title = "Click to sort · Shift-click to add a sort level";
    th.onclick = (e) => onHeaderClick(c, e.shiftKey);
    thead.appendChild(th);
  }
  const tbody = $("#tbody");
  tbody.innerHTML = "";
  for (const row of pageRows) {
    const tr = document.createElement("tr");
    if (row._k) tr.dataset.k = row._k;
    const cstat = collectionStatus(row);
    if (cstat) tr.className = "row-col-" + cstat;
    const cover = ENRICH_ENABLED ? `<td class="cover">${coverCell(row)}</td>` : "";
    tr.innerHTML = cover + cols.map((c) => `<td>${fmtCell(row[c.key], c.type)}</td>`).join("");
    tr.onclick = () => openDrawer(row);
    tbody.appendChild(tr);
  }
}

// Completed games get a green-bordered card. The Completed tab is all finished;
// on the Games tab it's the per-row Completed flag.
function rowCompleted(row) {
  if (activeTab === "completed") return true;
  if (activeTab === "games") return !!row.completed;
  return false;
}

const CARD_ROW = new WeakMap();   // card element -> row (for in-place patching)

// When an explicit sort is active, surface the sorted field's value on the card
// so you can see what you're sorting by without opening the game.
function sortValueHtml(row) {
  const st = tabState[activeTab];
  if (!st || !st.sort || !st.sort.length) return "";     // default sort → nothing extra
  return st.sort.slice(0, 2).map((s) => {
    const c = colByKey(s.key);
    if (!c) return "";
    const v = row[s.key];
    const val = (v === undefined || v === null || v === "")
      ? `<i class="muted">—</i>` : fmtCell(v, c.type);
    return `<div class="card-sortval"><span>${escapeHtml(c.label)}</span>${val}</div>`;
  }).join("");
}

// Text-only card body (no <img>), so it can be re-rendered without flicker.
function cardBodyHtml(row) {
  const titleKey = (columns().find((c) => c.primary) || columns()[0]).key;
  const title = escapeHtml(String(row[titleKey] ?? "Untitled"));
  const rel = row.releaseDate || row.release;                 // full date, else year
  const relDisp = rel ? fmtDate(rel) : row.releaseYear;
  const pt = playtimeOf(row);
  const parts = [row.platform, relDisp].filter((x) => x != null && x !== "").map((x) => escapeHtml(String(x)));
  if (pt != null) parts.push("⏱ " + fmtHours(pt));
  const cv = collectionValueOf(row);
  if (cv != null) parts.push("$" + cv.toFixed(2));
  if (row._members && row._members.length > 1) parts.push(`⧉ ${row._members.length} copies`);
  const units = salesOf(row);
  if (units != null) parts.push("↗ " + fmtUnits(units));
  const rating = row.rating != null
    ? `<span class="card-rating ${ratingClass(row.rating)}" title="My rating">${Math.round(row.rating * 100)}</span>` : "";
  const mc = metacriticOf(row);
  const meta = mc != null
    ? `<span class="card-meta ${ratingClass(mc)}" title="Metacritic">${Math.round(mc * 100)}</span>` : "";
  // Title + platform/year always visible on the scrim; the rest (playtime,
  // value, sales, sorted-by field, collection badge) unfurls on hover.
  const head = [row.platform, relDisp].filter((x) => x != null && x !== "").map((x) => escapeHtml(String(x)));
  const extra = parts.slice(head.length);
  return `${meta}${rating}<div class="card-title" title="${title}">${title}</div>` +
    `<div class="card-sub">${head.join(" · ")}</div>` +
    `<div class="card-extra"><div>` +
      (extra.length ? `<div class="card-sub">${extra.join(" · ")}</div>` : "") +
      collectionBadgeHtml(row) + sortValueHtml(row) +
    `</div></div>`;
}

function renderGrid(pageRows) {
  const grid = $("#grid");
  stopPreview();                 // the card it was attached to is about to vanish
  grid.innerHTML = "";
  pageRows.forEach((row, i) => {
    const cs = coverSrc(ENRICH[row._k], "cover_big");
    const pend = coverPending(row);
    const pixel = coverIsPixelArt(ENRICH[row._k], cs) ? " pixel" : "";
    const cover = cs
      ? `<img class="card-cover${pixel}" loading="lazy" src="${cs}" alt="">`
      : `<div class="card-cover ph${pend ? " skel" : ""}">${pend ? "" : icon("i-library", 26)}</div>`;
    const card = document.createElement("div");
    card.style.setProperty("--i", Math.min(i, 24) * 22 + "ms");   // fan-in stagger
    // A part-finished collection is yellow, and that beats the green "done"
    // ring — the compilation itself isn't finished even on the Completed tab.
    const cstat = collectionStatus(row);
    card.className = "card" + (cstat === "partial" ? " partial"
      : (cstat === "complete" || rowCompleted(row)) ? " done" : "");
    if (row._k) card.dataset.k = row._k;
    CARD_ROW.set(card, row);
    card.innerHTML = `${cover}<div class="card-body">${cardBodyHtml(row)}</div>`;
    card.onclick = () => openDrawer(row);
    wirePreview(card);
    grid.appendChild(card);
  });
}

// ---- YouTube: know whether it actually played ----------------------------
// YouTube serves some clients a "sign in to confirm you're not a bot" wall
// inside the embed. We can't overrule that, and we can't read a cross-origin
// iframe to detect it — but we CAN ask the player whether it started, using the
// IFrame API's postMessage handshake. If it never reaches the playing state, the
// embed is useless: tear it down and show something the user can actually click.
const YT_ORIGIN = location.origin;
const YT_TIMEOUT = 4500;
let ytFailures = 0;               // consecutive; a play resets it
const YT_GIVE_UP = 4;             // dead trailers are common; a wall is not
let YT_BLOCKED = false;         // once YouTube is clearly refusing us, stop trying

const ytSrc = (id, opts = {}) => {
  const p = new URLSearchParams({
    rel: "0", modestbranding: "1", playsinline: "1",
    enablejsapi: "1", origin: YT_ORIGIN, ...opts,
  });
  return `https://www.youtube.com/embed/${id}?${p}`;
};

// Watch a player: onPlay() the moment it truly starts, onFail() if it never does
// within YT_TIMEOUT.
function ytWatch(frame, onFail, onPlay) {
  let done = false;
  const onMsg = (e) => {
    if (!/youtube(-nocookie)?\.com$/.test(new URL(e.origin).hostname.replace(/^www\./, ""))) return;
    if (e.source !== frame.contentWindow) return;
    let d;
    try { d = typeof e.data === "string" ? JSON.parse(e.data) : e.data; } catch (_) { return; }
    const state = d && d.info && d.info.playerState;
    if (state === 1 || state === 3) {      // playing / buffering: it's alive
      done = true;
      ytFailures = 0;
      cleanup();
      if (onPlay) onPlay();
    }
  };
  const cleanup = () => {
    window.removeEventListener("message", onMsg);
    clearTimeout(timer);
    clearInterval(poke);
  };
  window.addEventListener("message", onMsg);

  // The player only starts reporting once we say hello.
  const poke = setInterval(() => {
    try {
      frame.contentWindow.postMessage('{"event":"listening","id":1,"channel":"widget"}', "*");
    } catch (_) { /* not loaded yet */ }
  }, 400);

  const timer = setTimeout(() => {
    cleanup();
    if (done) return;
    // Enough consecutive strikes and we stop asking: if YouTube is walling this
    // client, every further embed is just another wall. But a single dead embed
    // is NOT that — IGDB's video ids go stale (deleted trailers, embedding turned
    // off), and those fail exactly the same way from out here. Give it real
    // evidence of a pattern before writing the feature off; any success resets.
    if (++ytFailures >= YT_GIVE_UP) YT_BLOCKED = true;
    onFail();
  }, YT_TIMEOUT);
  return cleanup;
}

// ---- hover-to-play trailer previews --------------------------------------
// Hover a card and its trailer plays, muted, from a random point in the middle —
// the opening seconds of a trailer are logos, so starting at 0 would show you a
// publisher ident every time. Leaving the card puts the box art back.
//
// Guarded by hover-intent (a moment's dwell), so scrolling across the grid
// doesn't spawn twenty iframes; by a pointer check, since there's no hover on
// touch; and by prefers-reduced-motion.
const WANTS_MOTION = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const PREVIEW_DELAY = 550;                 // dwell before we commit to loading
let previewTimer = null, previewCard = null, previewWatch = null;

// Where in the trailer to start. Rolled once per video and then remembered, so
// hovering the same card twice shows you the same moment — a clip that jumps
// somewhere new on every hover reads as a glitch, not as variety. Still rolled
// fresh on each page load, so it isn't the same forever.
const PREVIEW_START = new Map();
const previewStart = (vid) => {
  if (!PREVIEW_START.has(vid)) {
    // Somewhere in the middle: trailers open on publisher logos, so 0 would show
    // an ident every time.
    PREVIEW_START.set(vid, 15 + Math.floor(Math.random() * 45));
  }
  return PREVIEW_START.get(vid);
};

function stopPreview() {
  clearTimeout(previewTimer);
  previewTimer = null;
  // Cancel the bot-wall watchdog FIRST. Without this, hovering off a card before
  // the video has started still lets the watchdog fire 4.5s later and record a
  // failure against YouTube — so two impatient hovers on a slow connection would
  // trip YT_BLOCKED and kill previews for the rest of the session.
  if (previewWatch) { previewWatch(); previewWatch = null; }
  if (!previewCard) return;
  const frame = previewCard.querySelector(".card-preview");
  if (frame) frame.remove();
  previewCard.classList.remove("previewing", "playing");
  previewCard = null;
}

function startPreview(card) {
  if (YT_BLOCKED) return;                 // YouTube isn't letting this client play
  const row = CARD_ROW.get(card);
  const vid = row && (ENRICH[row._k] || {}).video;
  if (!vid || card === previewCard) return;
  stopPreview();
  previewCard = card;
  const frame = document.createElement("iframe");
  frame.className = "card-preview";
  frame.src = ytSrc(vid, {
    autoplay: "1", mute: "1", controls: "0", disablekb: "1",
    iv_load_policy: "3", fs: "0", start: String(previewStart(vid)),
    // Loop it. Left to run out, the player draws a big endscreen replay button
    // over the card — and a preview that stops isn't a preview.
    loop: "1", playlist: vid,
  });
  frame.allow = "autoplay; encrypted-media";
  frame.tabIndex = -1;
  frame.setAttribute("aria-hidden", "true");
  card.appendChild(frame);
  card.classList.add("previewing");
  // If YouTube shows a bot wall instead of playing, a dead iframe over the box
  // art is worse than no preview at all. Put the art back.
  // Hold the box art up until the video is genuinely PLAYING, then cross-fade.
  // YouTube flashes its own chrome (a big pause bezel) for the ~1.5s it spends
  // booting the player, and we cannot style that away through a cross-origin
  // iframe — so simply don't show it. If the embed never plays, the art stays.
  previewWatch = ytWatch(frame,
    () => { if (previewCard === card) stopPreview(); },
    () => { if (previewCard === card) card.classList.add("playing"); });
}

// Any surface that renders a .card can opt in: it just has to tell us which row
// the card is for. The grid does this as it builds; Home does it after the fact.
function wirePreviewFor(card, row) {
  CARD_ROW.set(card, row);
  wirePreview(card);
}

function wirePreview(card) {
  if (!WANTS_MOTION) return;
  // pointerenter tells us WHAT is hovering. A media query doesn't: headless
  // Chrome and plenty of real desktops report (hover: none), and a touch that
  // lingers would otherwise trigger a preview you never asked for.
  card.addEventListener("pointerenter", (e) => {
    if (e.pointerType !== "mouse") return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => startPreview(card), PREVIEW_DELAY);
  });
  card.addEventListener("pointerleave", () => {
    if (previewCard === card) stopPreview();
    else clearTimeout(previewTimer);
  });
}

// Enrichment arrived: update covers/badges IN PLACE. A full re-render would
// recreate every <img> and make the whole grid flicker on each poll.
function patchEnrichedCells() {
  document.querySelectorAll("#grid .card[data-k]").forEach((card) => {
    const row = CARD_ROW.get(card);
    if (!row) return;
    const cur = card.querySelector(".card-cover");
    const cs = coverSrc(ENRICH[card.dataset.k], "cover_big");
    if (cs && cur && cur.tagName !== "IMG") {          // placeholder → real cover
      const img = document.createElement("img");
      // Must re-apply .pixel here too: on first paint the enrichment hasn't
      // arrived, so every cover starts as a placeholder and is swapped in HERE.
      img.className = "card-cover" + (coverIsPixelArt(ENRICH[card.dataset.k], cs) ? " pixel" : "");
      img.loading = "lazy"; img.alt = ""; img.src = cs;
      cur.replaceWith(img);
    } else if (!cs && cur && cur.classList.contains("skel") &&
               (ENRICH_COMPLETE || NO_MATCH.has(card.dataset.k) || (card.dataset.k in ENRICH))) {
      cur.classList.remove("skel");                    // resolved, just no cover
      cur.innerHTML = icon("i-library", 26);
    }
    const body = card.querySelector(".card-body");
    if (body) body.innerHTML = cardBodyHtml(row);      // text only — safe to redraw
  });
  document.querySelectorAll("#tbody tr[data-k]").forEach((tr) => {
    const cs = coverSrc(ENRICH[tr.dataset.k], "cover_small");
    const cell = tr.querySelector("td.cover");
    if (cs && cell && !cell.querySelector("img")) {
      cell.innerHTML = `<img class="cover-thumb" loading="lazy" src="${cs}" alt="">`;
    }
  });
}

// Grid has no clickable headers — a Sort dropdown + direction toggle stand in.
function populateGridSort() {
  const sel = $("#gridsort");
  const games = activeTab === "games";
  const cols = games
    ? GAMES_SORT_MENU.map(sortMeta).filter(Boolean)
    : columns().filter((c) => c.sort).concat(VIRTUAL_SORTS.filter((v) => v.on()));
  const eff = effectiveSort();
  const usingDefault = !(tabState[activeTab].sort && tabState[activeTab].sort.length);
  // No "Default" entry on All Games: the default IS Release Date, so say so and
  // select it. A menu item called "Default" tells you nothing about what you get.
  const cur = usingDefault ? (games ? "releaseDate" : "__default") : eff[0].key;
  sel.innerHTML = (games ? "" : `<option value="__default">Default</option>`) +
    cols.map((c) => `<option value="${c.key}">${escapeHtml(sortLabel(c))}</option>`).join("");
  sel.value = cols.some((c) => c.key === cur) ? cur : (games ? "releaseDate" : "__default");
  $("#gridsortdir").textContent = eff[0].dir === "asc" ? "▲" : "▼";
  $("#gridsortdir").disabled = false;      // Release Date can be flipped like anything else
}

function renderPager(pages) {
  const st = tabState[activeTab];
  const el = $("#pager");
  el.innerHTML = "";
  if (pages <= 1) return;
  const go = (page) => {
    st.page = Math.min(pages, Math.max(1, page));
    renderTable(currentFiltered);
    $("#tablewrap").scrollTop = 0; $("#gridwrap").scrollTop = 0;
    window.scrollTo({ top: 0, behavior: "smooth" });
    nav();
  };
  const mk = (label, page, disabled, title) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.disabled = disabled;
    if (title) b.title = title;
    b.onclick = () => go(page);
    return b;
  };
  el.appendChild(mk("«", 1, st.page <= 1, "First page"));
  el.appendChild(mk("‹ Prev", st.page - 1, st.page <= 1));

  // Jump straight to a page — at 295 pages, paging one at a time is useless.
  const jump = document.createElement("span");
  jump.className = "page-jump";
  jump.innerHTML = `Page <input type="number" min="1" max="${pages}" value="${st.page}" aria-label="Page number"> of ${pages.toLocaleString()}`;
  const input = jump.querySelector("input");
  const commit = () => {
    const n = parseInt(input.value, 10);
    if (isFinite(n) && n !== st.page) go(n); else input.value = String(st.page);
  };
  input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } };
  input.onblur = commit;
  el.appendChild(jump);

  el.appendChild(mk("Next ›", st.page + 1, st.page >= pages));
  el.appendChild(mk("»", pages, st.page >= pages, "Last page"));
}

// A real empty state beats an empty grid.
function emptyState(title, hint, action) {
  return `<div class="empty">
    <div class="empty-art">${icon("i-library", 40)}</div>
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(hint)}</p>
    ${action ? `<button class="btn" id="emptyAction">${escapeHtml(action)}</button>` : ""}
  </div>`;
}

// ---- detail drawer ------------------------------------------------------
let drawerSheet = "games";
// Value cell, made a clickable filter link for facetable text/year columns.
function detailValue(c, v) {
  const facetable = c.facet && (c.type === "text" || c.type === "year");
  const cell = fmtCell(v, c.type);
  if (facetable) return `<a class="facet-link" data-fk="${c.key}" data-fv="${escapeHtml(String(v))}" title="Filter by ${escapeHtml(c.label)}">${cell}</a>`;
  return cell;
}

// Opening a game FROM inside the drawer — a copy in a group, a related game, a
// collection member — is navigation, and navigation needs a way back. Anything
// that opens a drawer on top of an open drawer goes through here.
let drawerStack = [];

function openDrawerFrom(row, sheetKey) {
  if (drawerRow) drawerStack.push({ row: drawerRow, sheet: drawerSheet });
  openDrawer(row, sheetKey, true);
}
function drawerBack() {
  const prev = drawerStack.pop();
  if (prev) openDrawer(prev.row, prev.sheet, true);
}
const drawerTitleOf = (row) => String(row.title || row.game || "back");

function openDrawer(row, sheetKey, keepStack) {
  stopPreview();
  if (!keepStack) drawerStack = [];       // a fresh open starts a fresh history
  applyCoverAccent(row);
  drawerSheet = sheetKey || (SPECIAL_TABS.includes(activeTab) ? "games" : activeTab);
  const cols = (DATA.sheets[drawerSheet] || DATA.sheets.games).columns;
  const titleCol = cols[0];
  const body = $("#drawerBody");
  const titleText = escapeHtml(String(row[titleCol.key] ?? "Untitled"));
  let html = heroHtml(row, titleText);
  if (ENRICH_ENABLED && row._k) html += `<div id="igdbDetail" class="igdb-detail"></div>`;

  // Box art override — same manual upload as the shelf, so a game's cover can be fixed
  // (or supplied outright) from any detail card. Offered for every real sheet row, not
  // just owned physical ones: a game IGDB never matched has no cover at all, and this is
  // the only way to give it one. Grouped collection cards have no single row to attach to.
  if (row._k && !row._collection) html += `<button class="sh-btn drawer-art" id="drawerArt">Manage box art</button>`;

  /* Your own history with the game was buried in the "Raw data" disclosure,
     alongside File Size and MAME Romset — and it's the most personal thing on the
     card: what you paid, when you started it, whether you finished, what you
     thought. It gets its own section now; the rest stays behind the disclosure. */
  const MINE = ["owned", "completed", "rating", "priority", "playingStatus", "playingProgress",
                "datePurchased", "purchasePrice", "condition", "format",
                "dateStarted", "dateCompleted", "completionTime", "notes"];
  const cell = (c, v) => {
    const isNotes = c.type === "text" && String(v).length > 140;
    return isNotes
      ? `<div class="detail-row notes"><div class="k">${escapeHtml(c.label)}</div><div class="v">${escapeHtml(String(v))}</div></div>`
      : `<div class="detail-row"><div class="k">${escapeHtml(c.label)}</div><div class="v">${detailValue(c, v)}</div></div>`;
  };

  let raw = "", mine = "";
  for (const c of cols) {
    if (c.key === titleCol.key || c.key === "platform") continue;
    const v = row[c.key];
    if (v === undefined || v === null || v === "") continue;
    if (MINE.includes(c.key)) mine += cell(c, v);
    else raw += cell(c, v);
  }
  if (mine && !row._collection) {
    html += `<div class="mine-sect">
      <h3>${icon("i-star", 15)} Your history with this game</h3>
      <div class="mine-rows">${mine}</div>
    </div>`;
  } else if (mine) {
    raw = mine + raw;          // a grouped card's values are aggregates, not yours
  }
  html += (typeof editionsHtml === "function" ? editionsHtml(row) : "");
  html += `<div id="relations"></div>`;
  html += collectionSectionHtml(row);
  // Sheet fields collapse behind a "Raw data" disclosure — the enriched view
  // leads. A grouped collection card has no sheet row of its own; its values are
  // aggregates over the members, so don't dress them up as raw data.
  if (raw && !row._collection) html += `<details class="raw-data"><summary>Raw data</summary>${raw}</details>`;
  body.innerHTML = html;
  const back = $("#drawerBack");
  const prev = drawerStack[drawerStack.length - 1];
  back.hidden = !prev;
  if (prev) {
    const t = drawerTitleOf(prev.row);
    back.textContent = `← ${t.length > 22 ? t.slice(0, 21) + "…" : t}`;
    back.title = `Back to ${t}`;
  }
  wireCollections(body);
  const artBtn = $("#drawerArt");
  if (artBtn) artBtn.onclick = () => {
    const key = `${row._k}#${String(row.releaseRegion || "").trim()}`;
    // If the shelf is already loaded, we know whether this game has an upload; if not,
    // default to "no" — the Remove button just won't show until the shelf's been opened.
    const g = (typeof SHELF !== "undefined" ? SHELF.games : []).find((x) => x.k === key);
    openCoverEditor({
      key, platform: row.platform, title: row[titleCol.key],
      hasUpload: g ? g.src === "upload" : false,
      caseDefault: g ? g.case : null, existing: g ? g.upload : null,
      onDone: () => { if (typeof SHELF !== "undefined") SHELF.loaded = false; },
    });
  };
  // A grouped card's members open individually — with a way back to the group.
  body.querySelectorAll("[data-rlc]").forEach((el) => {
    el.onclick = () => {
      const m = (row._members || [])[+el.dataset.rlc];
      if (m) openDrawerFrom(m, "games");
    };
  });
  $("#overlay").hidden = false;
  // Reset scroll AFTER the overlay is shown. Set while it's still display:none it never
  // takes — and the browser then restores the PREVIOUS game's scroll when it appears,
  // which is why one game's scroll position leaked into the next.
  $("#drawer").scrollTop = 0;
  drawerRow = row;
  syncScrollLock();                       // the page behind the drawer must not scroll
  if (ENRICH_ENABLED && row._k) loadDetail(row._k, $("#igdbDetail"), 0, row);
}
function closeDrawer() { $("#overlay").hidden = true; drawerStack = []; syncScrollLock(); }

// Lock the page behind a full-screen overlay. Pinning the body with position:fixed (and
// restoring the scroll offset on release) is the one approach that also holds on iOS
// Safari, where `overflow:hidden` on the body alone doesn't stop touch scrolling.
// syncScrollLock() is called by every overlay on open AND close, and locks iff any
// overlay is still up — so a cover editor closing over an open drawer keeps the lock,
// and the re-entrant openDrawer (navigation within the drawer) never double-locks.
let _scrollLockY = 0;
function anyOverlayOpen() {
  return !$("#overlay").hidden
    || !$("#lightbox").hidden
    || (typeof cmdk !== "undefined" && cmdk.open)
    || $("#facets").classList.contains("open")
    || !!document.querySelector(".ce-scrim")                        // cover editor
    || (typeof shCur !== "undefined" && shCur >= 0);               // shelf 3D pull
}
function syncScrollLock() {
  const on = anyOverlayOpen();
  const locked = document.documentElement.classList.contains("modal-open");
  if (on && !locked) {
    _scrollLockY = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.style.top = `-${_scrollLockY}px`;
    document.documentElement.classList.add("modal-open");
  } else if (!on && locked) {
    document.documentElement.classList.remove("modal-open");
    document.body.style.top = "";
    window.scrollTo(0, _scrollLockY);
  }
}

// Clicking a facet-link (in the drawer) filters that field on its sheet's tab.
function applyDrawerFacet(key, val) {
  const st = tabState[drawerSheet];
  if (!st) return;
  st.facets = { [key]: new Set([String(val)]) };
  st.search = ""; st.page = 1;
  closeDrawer();
  switchTab(drawerSheet);
  nav();
}

// ---- orchestration ------------------------------------------------------
let currentFiltered = [];
let lastGroupedCount = -1;      // so the grouped view repaints once enrichment lands
const SPECIAL_TABS = ["home", "stats", "pick", "challenges", "health", "groups", "shelf"];
function setSpecialMode(mode) {   // null | "home" | "stats" | "pick" | "challenges"
  const special = SPECIAL_TABS.includes(mode);
  $("#stats").hidden = mode !== "stats";
  $("#picker").hidden = mode !== "pick";
  $("#challenges").hidden = mode !== "challenges";
  $("#home").hidden = mode !== "home";
  $("#health").hidden = mode !== "health";
  $("#groups").hidden = mode !== "groups";
  $("#shelfview").hidden = mode !== "shelf";
  $(".resultbar").hidden = special;
  $("#pager").style.display = special ? "none" : "";
  document.querySelector(".facets").style.display = special ? "none" : "";
  // Filters/sort don't apply on Stats/Pick — leave only "back to top".
  $("#fabFilters").hidden = special;
  $("#fabSort").hidden = special;
  if (special) {
    setSheet(false); setFacets(false);
    $("#tablewrap").hidden = true;
    $("#gridwrap").hidden = true;
    $("#timeline").hidden = true;    // ← was left on screen, showing through Series
    $("#views").hidden = true;
  }
}

function renderAll() {
  if (activeTab === "home") { setSpecialMode("home"); renderHome(); return; }
  if (activeTab === "stats") { setSpecialMode("stats"); renderStats(); return; }
  if (activeTab === "pick") { setSpecialMode("pick"); renderPicker(); return; }
  if (activeTab === "challenges") { setSpecialMode("challenges"); renderChallenges(); return; }
  if (activeTab === "health") { setSpecialMode("health"); renderHealth(); return; }
  if (activeTab === "groups") { setSpecialMode("groups"); renderGroups(); return; }
  if (activeTab === "shelf") { setSpecialMode("shelf"); renderShelf(); return; }
  setSpecialMode(null);
  renderFacets();
  currentFiltered = groupCollections(filterRows(null));
  renderTable(currentFiltered);
}

// reset: a deliberate navigation to a tab (clicking it) starts clean. Filters you
// set on All Games shouldn't still be there when you come back to it later — the
// only state that should survive is what's in the URL, which is how back/forward
// and shared links restore a view on purpose.
function switchTab(tab, reset) {
  if (reset && tabState[tab]) {
    const keep = tabState[tab];
    tabState[tab] = { ...freshState(), view: keep.view, combine: keep.combine };
  }
  activeTab = tab;
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === tab);
  if (!SPECIAL_TABS.includes(tab)) $("#search").value = tabState[tab].search;
  renderAll();
}

// ---- URL state: back/forward + shareable/refreshable links ---------------
let applyingState = false;
function syncURL(push) {
  if (applyingState) return;
  const p = new URLSearchParams();
  if (activeTab !== "home") p.set("tab", activeTab);
  if (activeTab === "pick") {
    if (pickState.selector) p.set("sel", pickState.selector);
    if (pickState.param) p.set("pp", pickState.param);
    if (pickState.minutes) p.set("mins", String(pickState.minutes));
  } else if (activeTab === "groups") {
    if (groupState.kind) p.set("g", groupState.kind);
    if (groupState.open) p.set("gk", groupState.open);
  } else if (activeTab === "challenges") {
    if (chState.open) p.set("ch", chState.open);
  } else if (tabState[activeTab]) {
    // Guarded on tabState, not on a list of tab names. Home, Reviews and Health
    // have no row state, and the old `!== "stats"` test let them fall in here and
    // blow up on tabState["health"].view.
    const st = tabState[activeTab];
    if (st.view !== VIEW_DEFAULT[activeTab]) p.set("view", st.view);
    if (st.combine !== COMBINE_DEFAULT[activeTab]) p.set("combine", st.combine ? "1" : "0");
    if (PAGE_SIZE !== 50) p.set("ps", String(PAGE_SIZE));
    if (st.search) p.set("q", st.search);
    if (st.page > 1) p.set("page", String(st.page));
    if (st.sort && st.sort.length) p.set("sort", st.sort.map((s) => `${s.key}:${s.dir}`).join(","));
    for (const [k, set] of Object.entries(st.facets)) if (set && set.size) p.set("f." + k, [...set].join("~"));
  }
  const qs = p.toString();
  history[push ? "pushState" : "replaceState"]({}, "", qs ? "?" + qs : location.pathname);
}
function applyStateFromURL() {
  applyingState = true;
  const p = new URLSearchParams(location.search);
  let tab = p.get("tab") === "series" ? "groups" : p.get("tab");   // old links still work
  tab = ["home", "games", "completed", "onOrder", "groups", "stats", "pick", "challenges", "health", "shelf"].includes(tab) ? tab : "home";
  if (SPECIAL_TABS.includes(tab)) {
    if (tab === "pick") { pickState.selector = p.get("sel") || pickState.selector; pickState.param = p.get("pp") || ""; pickState.minutes = +(p.get("mins") || 0); }
    if (tab === "challenges") { chState.open = p.get("ch") || null; chState.showAll = null; }
    if (tab === "groups") {
      // ?fr=<franchise> was the old Series link; it means the franchise axis.
      const legacy = p.get("fr");
      groupState.kind = legacy ? "series" : (p.get("g") || null);
      groupState.open = legacy || p.get("gk") || null;
      groupState.q = "";
    }
    applyingState = false; switchTab(tab); return;
  }
  const st = tabState[tab];
  st.view = ["table", "grid", "timeline"].includes(p.get("view")) ? p.get("view") : VIEW_DEFAULT[tab];
  st.combine = p.has("combine") ? p.get("combine") === "1" : COMBINE_DEFAULT[tab];
  PAGE_SIZE = parseInt(p.get("ps"), 10) || 50;
  st.search = p.get("q") || "";
  st.page = parseInt(p.get("page"), 10) || 1;
  const sort = p.get("sort");
  st.sort = sort ? sort.split(",").map((s) => { const [key, dir] = s.split(":"); return { key, dir: dir === "asc" ? "asc" : "desc" }; }) : null;
  st.facets = {};
  for (const [k, v] of p.entries()) if (k.startsWith("f.")) st.facets[k.slice(2)] = new Set(v.split("~"));
  $("#pagesize").value = String(PAGE_SIZE);
  applyingState = false;
  switchTab(tab);
}
const nav = () => syncURL(true);
window.addEventListener("popstate", applyStateFromURL);

function setFreshness() {
  const m = DATA.meta || {};
  const el = $("#freshness");
  if (!m.lastUpdated) { el.textContent = ""; return; }
  const when = new Date(m.lastUpdated).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  el.innerHTML = `data as of<br>${escapeHtml(when)}`;
  if (m.lastError) {
    const banner = $("#banner");
    banner.hidden = false;
    banner.textContent = `⚠ Last Dropbox refresh failed (${m.lastError}). Showing last-known data.`;
  }
}

// ---- boot ---------------------------------------------------------------
async function load() {
  showSkeletons();
  let payload = null;
  for (let attempt = 0; attempt < 40; attempt++) {
    const res = await fetch("api/data", { cache: "no-store" });
    if (res.ok) { payload = await res.json(); break; }
    if (res.status === 503) { $("#count").textContent = "Fetching spreadsheet from Dropbox…"; await sleep(1500); continue; }
    throw new Error(`api/data returned ${res.status}`);
  }
  if (!payload) { $("#count").textContent = "Could not load data — is the Dropbox link set?"; return; }
  DATA = payload;
  resetCollections();
  resetHealth();
  resetSearchCache();
  resetGroups();
  resetTaste();
  resetRelations();
  for (const k of Object.keys(_cmdkFacets)) delete _cmdkFacets[k];
  const en = DATA.meta && DATA.meta.enrichment;
  ENRICH_ENABLED = !!(en && en.enabled !== false);
  ENRICH_SOURCES = en && en.sources ? Object.keys(en.sources) : [];
  if (ENRICH_ENABLED) updateEnrichStatus(en);
  setFreshness();
  applyStateFromURL();          // restore tab/filters/sort/view from the URL
  loadAllEnrichment();          // global covers + IGDB facets (polls during backfill)
  loadRomm();                   // which games we can actually play in the browser
  loadPrefs();                  // saved views + custom challenges follow you between browsers
  loadValueHistory();           // daily collection-value snapshots (for the trend chart)
  loadRecs();                   // "because you liked …"
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// events
// ---- Stats dashboard (hand-rolled SVG, no deps) -------------------------
const PALETTE = ["#7c5cff", "#22d3ee", "#34d399", "#fbbf24", "#f472b6", "#60a5fa", "#fb923c", "#a78bfa", "#2dd4bf", "#f87171", "#e879f9", "#facc15"];
function countBy(arr) {
  const m = new Map();
  for (const v of arr) { if (v == null || v === "") continue; m.set(v, (m.get(v) || 0) + 1); }
  return m;
}
function topCounts(arr, n) {
  return [...countBy(arr).entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([label, value]) => ({ label, value }));
}
function svgBarsH(data, width = 340, barH = 20, gap = 7, fmt = (v) => v.toLocaleString()) {
  if (!data.length) return `<div class="s-empty">No data</div>`;
  const max = Math.max(1, ...data.map((d) => d.value));
  const labelW = 130, valW = 52, chartW = width - labelW - valW, h = data.length * (barH + gap);
  let y = 0, out = "";
  data.forEach((d, i) => {
    const w = Math.max(2, chartW * d.value / max);
    out += `<g transform="translate(0,${y})"><text x="${labelW - 6}" y="${barH / 2}" dy="0.35em" text-anchor="end" class="s-lbl">${escapeHtml(String(d.label))}</text>` +
      `<rect x="${labelW}" y="1" width="${w.toFixed(1)}" height="${barH - 2}" rx="3" fill="${PALETTE[i % PALETTE.length]}"/>` +
      `<text x="${(labelW + w + 5).toFixed(1)}" y="${barH / 2}" dy="0.35em" class="s-val">${escapeHtml(fmt(d.value))}</text></g>`;
    y += barH + gap;
  });
  return `<svg viewBox="0 0 ${width} ${h}" class="s-svg" preserveAspectRatio="xMinYMin meet">${out}</svg>`;
}
function svgBarsV(data, width = 360, height = 170, color = PALETTE[0]) {
  if (!data.length) return `<div class="s-empty">No data</div>`;
  const max = Math.max(1, ...data.map((d) => d.value)), n = data.length, bw = width / n;
  let out = "";
  data.forEach((d, i) => {
    const bh = (height - 26) * d.value / max;
    out += `<g transform="translate(${(i * bw).toFixed(1)},0)">` +
      `<rect x="${(bw * 0.15).toFixed(1)}" y="${(height - 26 - bh).toFixed(1)}" width="${(bw * 0.7).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${color}"/>` +
      `<text x="${(bw / 2).toFixed(1)}" y="${height - 9}" text-anchor="middle" class="s-axis">${escapeHtml(String(d.label))}</text>` +
      (d.value ? `<text x="${(bw / 2).toFixed(1)}" y="${(height - 28 - bh).toFixed(1)}" text-anchor="middle" class="s-val">${d.value}</text>` : "") + `</g>`;
  });
  return `<svg viewBox="0 0 ${width} ${height}" class="s-svg">${out}</svg>`;
}
function svgDonut(segments, size = 150) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1, r = size / 2, rin = r * 0.58;
  let a0 = -Math.PI / 2, paths = "";
  segments.forEach((s, i) => {
    if (!s.value) return;
    const a1 = a0 + 2 * Math.PI * s.value / total, large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (ang, rad) => [(r + rad * Math.cos(ang)).toFixed(2), (r + rad * Math.sin(ang)).toFixed(2)];
    const [x0, y0] = p(a0, r), [x1, y1] = p(a1, r), [xi0, yi0] = p(a1, rin), [xi1, yi1] = p(a0, rin);
    paths += `<path d="M${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} L${xi0},${yi0} A${rin},${rin} 0 ${large} 0 ${xi1},${yi1} Z" fill="${PALETTE[i % PALETTE.length]}"/>`;
    a0 = a1;
  });
  const legend = segments.filter((s) => s.value).map((s, i) =>
    `<div class="s-leg"><span style="background:${PALETTE[i % PALETTE.length]}"></span>${escapeHtml(String(s.label))} <b>${s.value}</b></div>`).join("");
  return `<div class="s-donut-wrap"><svg viewBox="0 0 ${size} ${size}" class="s-donut">${paths}</svg><div class="s-legend">${legend}</div></div>`;
}
// A numeric value counts up on scroll-in (data-n); anything else renders as-is.
/* A stat card. Eleven of these used to be identical — same size, same accent
   gradient on every number — so nothing led and nothing receded, and the accent
   was doing the job state colour should do.

   opts.tone   "lead" | "good" | "warn" | "" — colour carries MEANING now.
   opts.icon   an icon id, set back in the corner.
   opts.sub    the line under the number that says what it's of. */
const statCard = (v, l, pre = "", post = "", opts = {}) => {
  const { tone = "", icon: ic = "", sub = "" } = opts;
  const num = typeof v === "number" && isFinite(v);
  const body = num
    ? `<div class="s-num" data-n="${v}" data-pre="${escapeHtml(pre)}" data-post="${escapeHtml(post)}">${escapeHtml(pre)}0${escapeHtml(post)}</div>`
    : `<div class="s-num">${v == null ? "—" : escapeHtml(String(v))}</div>`;
  return `<div class="stat-card${tone ? " t-" + tone : ""}">
    ${ic ? `<span class="s-ico">${icon(ic, 15)}</span>` : ""}
    ${body}
    <div class="s-cap">${escapeHtml(l)}</div>
    ${sub ? `<div class="s-sub">${escapeHtml(sub)}</div>` : ""}
  </div>`;
};
const last = (pts) => (pts && pts.length ? pts[pts.length - 1].value.toLocaleString() : "0");

const statPanel = (title, body, cls = "", note = "") =>
  `<div class="stat-panel ${cls}"><h3>${escapeHtml(title)}</h3>${
    note ? `<p class="s-note">${escapeHtml(note)}</p>` : ""}${body}</div>`;

const usd = (v) => "$" + Math.round(v).toLocaleString();
const yr2 = (y) => `'${String(y).slice(2)}`;
const yearOf = (iso) => (typeof iso === "string" && /^\d{4}/.test(iso) ? +iso.slice(0, 4) : null);
const bucketize = (data, buckets, val) => buckets.map(([label, lo, hi]) => ({ label, value: data.filter((r) => { const v = val(r); return v != null && v >= lo && v < hi; }).length }));

// ---- Year in review + backlog burn-down ---------------------------------
const statsState = { year: null };
let VALUE_HISTORY = null;          // [{day,total,games,priced}] — daily snapshots
let RECS = null;                   // "because you liked …" (see src/recommend.py)

async function loadRecs() {
  try {
    const res = await fetch("api/recommendations");
    const j = await res.json();
    RECS = j.items || [];
    if (activeTab === "home") renderHome();
  } catch (_) { RECS = []; }
}

// GameEye only knows today's price, so the trend has to be recorded as it
// happens (see enrich.snapshot_value). One point per day; useless on day one.
async function loadValueHistory() {
  try {
    const res = await fetch("api/value-history");
    const j = await res.json();
    VALUE_HISTORY = j.history || [];
    if (activeTab === "stats" && VALUE_HISTORY.length > 1) renderStats();
  } catch (_) { VALUE_HISTORY = []; }
}

function yearInReview(rows, games) {
  const years = [...new Set(rows.map((r) => yearOf(r.date)).filter(Boolean))].sort((a, b) => b - a);
  if (!years.length) return "";
  if (statsState.year == null || !years.includes(statsState.year)) statsState.year = years[0];
  const y = statsState.year;
  const mine = rows.filter((r) => yearOf(r.date) === y);
  const prev = rows.filter((r) => yearOf(r.date) === y - 1);

  const hours = mine.reduce((a, r) => a + (r.playTime || 0), 0);
  const rated = mine.filter((r) => r.rating != null);
  const avg = rated.length ? rated.reduce((a, r) => a + r.rating, 0) / rated.length : null;
  const best = rated.slice().sort((a, b) => b.rating - a.rating)[0];
  const worst = rated.slice().sort((a, b) => a.rating - b.rating)[0];
  const longest = mine.filter((r) => r.playTime).sort((a, b) => b.playTime - a.playTime)[0];
  const delta = prev.length ? mine.length - prev.length : null;
  const deltaTxt = delta == null ? "" :
    `<span class="yr-delta ${delta >= 0 ? "up" : "down"}">${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)} vs ${y - 1}</span>`;

  const gameChip = (r, label) => r
    ? `<button class="yr-game" data-yg="${escapeHtml(String(r._k || ""))}">
         <span class="yr-game-l">${escapeHtml(label)}</span>
         <b>${escapeHtml(String(r.game))}</b>
         <span class="muted">${r.rating != null ? Math.round(r.rating * 100) + "%" : ""}${r.playTime ? ` · ${fmtHours(r.playTime)}` : ""}</span>
       </button>` : "";

  // Day-by-day, not month-by-month: monthly bars hid the fact that completions
  // come in bursts.
  const byDay = {}, gamesByDay = {};
  mine.forEach((r) => {
    if (!/^\d{4}-\d{2}-\d{2}/.test(String(r.date))) return;
    const d = String(r.date).slice(0, 10);
    byDay[d] = (byDay[d] || 0) + 1;
    (gamesByDay[d] = gamesByDay[d] || []).push(String(r.game));
  });
  // A count tells you a day was busy; the names tell you what the day WAS.
  const dayTip = (iso, n) => tipList(`${fmtDate(iso)} — ${n} finished`, gamesByDay[iso] || []);
  const showDay = (iso) => {
    const st = tabState.completed;
    st.facets = {}; st.search = ""; st.sort = null; st.page = 1;
    switchTab("completed");
    nav();
  };
  const top = rated.slice().sort((a, b) => b.rating - a.rating).slice(0, 10);

  return `<section class="yr">
    <div class="yr-head">
      <h2>${y} in review</h2>
      <label class="ctl">Year
        <select id="yrPick">${years.map((v) => `<option value="${v}"${v === y ? " selected" : ""}>${v}</option>`).join("")}</select>
      </label>
    </div>
    <div class="yr-grid">
      <div class="yr-cards">
        ${statCard(mine.length, "Games finished")}
        ${statCard(Math.round(hours), "Hours played", "", "h")}
        ${statCard(avg != null ? Math.round(avg * 100) : null, "Avg rating", "", "%")}
        ${statCard(mine.filter((r) => r.vr).length, "In VR")}
      </div>
      ${deltaTxt}
      <div class="yr-games">
        ${gameChip(best, "Favourite")}
        ${gameChip(longest, "Longest")}
        ${gameChip(worst, "Least favourite")}
      </div>
    </div>
    ${statPanel(`Every day of ${y}`, heatmap(byDay, y, { onDay: showDay, tipFor: dayTip }), "wide")}
    ${statPanel(`The best of ${y}`, posterRow(top, { note: (r) => `${Math.round(r.rating * 100)}%` }), "wide")}
    ${statPanel(`What you played in ${y}`, barsH(topCounts(mine.map((r) => r.genre), 7)))}
    ${statPanel(`Where you played in ${y}`, barsH(topCounts(mine.map((r) => r.platform), 8)))}
  </section>`;
}

// At your recent pace, how long does the backlog actually take?
function burnDown(rows, games) {
  const years = [...new Set(rows.map((r) => yearOf(r.date)).filter(Boolean))].sort((a, b) => b - a);
  const recent = years.slice(0, 3);
  if (!recent.length) return "";
  const rate = recent.reduce((a, y) => a + rows.filter((r) => yearOf(r.date) === y).length, 0) / recent.length;
  const backlog = games.filter((r) => !r.completed).length;
  const yrs = rate ? backlog / rate : Infinity;
  const now = new Date().getFullYear();

  // The same backlog, if you were choosier about length.
  const scen = [
    ["Everything", backlog],
    ["Under 20h only", games.filter((r) => !r.completed && (playtimeOf(r) ?? 99) < 20).length],
    ["Under 10h only", games.filter((r) => !r.completed && (playtimeOf(r) ?? 99) < 10).length],
    ["Under 5h only", games.filter((r) => !r.completed && (playtimeOf(r) ?? 99) < 5).length],
    ["Owned only", games.filter((r) => !r.completed && r.owned).length],
  ].map(([label, n]) => ({ label, value: Math.round(n / rate), n }));

  return `<section class="yr">
    <div class="yr-head"><h2>Backlog burn-down</h2></div>
    <p class="yr-note">You finished <b>${Math.round(rate)}</b> games a year over ${recent.length === 1 ? "the last year" : `${recent.length} years`}.
      At that pace the <b>${backlog.toLocaleString()}</b>-game backlog runs out in
      <b>${isFinite(yrs) ? Math.round(yrs).toLocaleString() : "∞"} years</b> — around <b>${isFinite(yrs) ? now + Math.round(yrs) : "never"}</b>.</p>
    ${statPanel("Years to clear the backlog", barsH(scen, { fmt: (v) => v.toLocaleString() + " yrs" }), "wide")}
  </section>`;
}

// Show the prediction model's homework. A model that can't state its own error
// bar is asking to be trusted on vibes.
function predictionPanel() {
  const m = typeof tasteModel === "function" ? tasteModel() : null;
  if (!m || !m.ok) return "";
  const e = m.eval;
  const pts = (v) => (v * 100).toFixed(1);
  const beatsCritics = e.liftVsCritic > 0;
  return `<h2 class="stat-sec">Predicted ratings</h2>
    <div class="stat-grid">
      <div class="stat-panel wide">
        <h3>How good is the guess?</h3>
        <p class="yr-note">
          Trained on your <b>${m.n.toLocaleString()}</b> rated games and tested on
          <b>${e.tested.toLocaleString()}</b> it never saw. It is off by
          <b>${pts(e.mae)} points</b> on average — against <b>${pts(e.maeMean)}</b> if we
          just guessed your average every time, and <b>${pts(e.maeCritic)}</b> if we simply
          quoted Metacritic. So it is <b>${(e.liftVsMean * 100).toFixed(0)}%</b> better than
          guessing${beatsCritics
            ? ` and <b>${(e.liftVsCritic * 100).toFixed(0)}%</b> better than the critics`
            : `, but <b>not</b> better than just quoting the critics — treat it with suspicion`}.
        </p>
        ${barsH([
          { label: "This model", value: +pts(e.mae) },
          { label: "Just use Metacritic", value: +pts(e.maeCritic) },
          { label: "Guess your average", value: +pts(e.maeMean) },
        ], { fmt: (v) => v + " pts off" })}
      </div>
    </div>`;
}

function renderStats() {
  const rows = (DATA.sheets.completed || { rows: [] }).rows;
  const games = ((DATA.sheets.games || {}).rows) || [];
  const host = $("#stats");
  if (!rows.length && !games.length) { host.innerHTML = emptyState("No data yet", "The spreadsheet hasn’t loaded."); return; }
  resetChartLinks();

  // Counts of a field, as bars that filter that tab when clicked.
  // A bar is a pile of games. Say which ones — a count you can't interrogate is
  // just a number.
  const nameOf = (r) => String(r.game || r.title || "");
  const countBars = (src, field, n, tab) =>
    topCounts(src.map((r) => r[field]), n).map((d) => {
      const members = src.filter((r) => r[field] === d.label);
      return { ...d, link: facetLink(tab, field, d.label),
               tip: tipList(`${d.label} — ${d.value.toLocaleString()} games`, members.map(nameOf), members.length) };
    });

  // --- completed ---
  const hours = rows.reduce((a, r) => a + (r.playTime || 0), 0);
  const rated = rows.filter((r) => r.rating != null);
  const avg = rated.length ? rated.reduce((a, r) => a + r.rating, 0) / rated.length : null;
  const critRated = rows.filter((r) => r.criticScore != null);
  const avgCrit = critRated.length ? critRated.reduce((a, r) => a + r.criticScore, 0) / critRated.length : null;
  const years = rows.map((r) => yearOf(r.date)).filter(Boolean);
  const curYear = years.length ? Math.max(...years) : 0;
  const thisYear = years.filter((y) => y === curYear).length;
  const byYear = countBy(years);
  const yearData = [...byYear.keys()].sort((a, b) => a - b).map((y) => ({ label: yr2(y), value: byYear.get(y) }));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const bm = new Array(12).fill(0);
  rows.forEach((r) => { if (typeof r.date === "string" && /^\d{4}-\d{2}/.test(r.date)) bm[+r.date.slice(5, 7) - 1]++; });
  const monthData = MONTHS.map((m, i) => ({ label: m, value: bm[i] }));
  const decades = countBy(rows.map((r) => (r.releaseYear && /^\d/.test(String(r.releaseYear)) ? Math.floor(+r.releaseYear / 10) * 10 : null)));
  const decadeData = [...decades.keys()].sort((a, b) => a - b).map((d) => ({ label: `${d}s`, value: decades.get(d) }));
  const ratingData = bucketize(rows, [["90–100", .9, 1.01], ["80–89", .8, .9], ["70–79", .7, .8], ["60–69", .6, .7], ["< 60", -1, .6]], (r) => r.rating);

  // Game-level charts link straight to the game.
  const longest = rows.filter((r) => r.playTime).sort((a, b) => b.playTime - a.playTime).slice(0, 10)
    .map((r) => ({ label: r.game, value: Math.round(r.playTime), link: gameLink(r, "completed") }));
  const gaps = rows.filter((r) => r.criticScore != null && r.rating != null)
    .map((r) => ({ label: r.game, value: Math.round((r.rating - r.criticScore) * 100), link: gameLink(r, "completed") }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value)).slice(0, 10);
  const best = rows.filter((r) => r.rating != null).sort((a, b) => b.rating - a.rating).slice(0, 10)
    .map((r) => ({ label: r.game, value: Math.round(r.rating * 100), link: gameLink(r, "completed") }));
  // Running total of everything ever finished, set against the two ways a game
  // arrives. A finishing rate on its own says nothing; the GAP between the lines
  // is the backlog, drawn.
  //
  //   Acquired  — Date Purchased. 2008-2026, the long history.
  //   Added     — Date Added. Flat until 2024 because that is when the column
  //               started being filled in, which is honest rather than broken.
  const cumFrom = (src, dateOf) => {
    const per = new Map();
    for (const r of src) { const y = yearOf(dateOf(r)); if (y) per.set(y, (per.get(y) || 0) + 1); }
    let run = 0;
    return [...per.keys()].sort((a, b) => a - b).map((yy) => {
      run += per.get(yy);
      // x is the real year: this axis is shared with series that start in other
      // years, and "'07" sorts as a string in ways nobody wants.
      return { x: yy, label: yr2(yy), value: run,
               tip: `${yy} — ${run.toLocaleString()} (+${per.get(yy).toLocaleString()} that year)` };
    });
  };
  const cumulative = cumFrom(rows, (r) => r.date);
  const cumAcquired = cumFrom(games.filter((r) => r.purchasePrice != null), (r) => r.datePurchased);
  const cumAdded = cumFrom(games, (r) => r.dateAdded);
  // Every rated game as a dot against the critics.
  const scatterPts = rows
    .filter((r) => r.rating != null && r.criticScore != null)
    .map((r) => ({ x: r.criticScore, y: r.rating, label: String(r.game), link: gameLink(r, "completed") }));
  // Taste profile: average score per genre, for genres you've played enough of.
  const gSum = new Map(), gN = new Map();
  for (const r of rows) {
    if (!r.genre || r.rating == null) continue;
    gSum.set(r.genre, (gSum.get(r.genre) || 0) + r.rating);
    gN.set(r.genre, (gN.get(r.genre) || 0) + 1);
  }
  const genreRadar = [...gN.entries()].filter(([, n]) => n >= 15)
    .map(([g, n]) => ({
      label: g, value: gSum.get(g) / n,
      tip: `${g}\nYour average: ${Math.round((gSum.get(g) / n) * 100)}%\nAcross ${n} finished games`,
    }))
    .sort((a, b) => b.value - a.value).slice(0, 8);
  const bestRows = rows.filter((r) => r.rating != null).sort((a, b) => b.rating - a.rating).slice(0, 12);

  const flags = [
    { label: "Steam Deck", value: rows.filter((r) => r.steamDeck).length },
    { label: "Emulated", value: rows.filter((r) => r.emulated).length },
    { label: "VR", value: rows.filter((r) => r.vr).length },
  ];

  // --- backlog ---
  const backlog = games.filter((r) => !r.completed);
  const backlogHours = backlog.reduce((a, r) => a + (playtimeOf(r) || 0), 0);
  const complPct = games.length ? Math.round(100 * games.filter((r) => r.completed).length / games.length) : 0;
  const backlogTime = bucketize(backlog, [["< 2h", -1, 2], ["2–5h", 2, 5], ["5–10h", 5, 10], ["10–20h", 10, 20], ["20–40h", 20, 40], ["40h+", 40, 1e9]], playtimeOf);

  // --- purchases & value ---
  const purchases = games.filter((r) => r.purchasePrice != null && yearOf(r.datePurchased));
  const spendMap = new Map(), boughtMap = new Map();
  purchases.forEach((r) => { const y = yearOf(r.datePurchased); spendMap.set(y, (spendMap.get(y) || 0) + r.purchasePrice); boughtMap.set(y, (boughtMap.get(y) || 0) + 1); });
  const spendData = [...spendMap.keys()].sort((a, b) => a - b).map((y) => ({ label: yr2(y), value: Math.round(spendMap.get(y)) }));
  const boughtData = [...boughtMap.keys()].sort((a, b) => a - b).map((y) => ({ label: yr2(y), value: boughtMap.get(y) }));
  const totalSpent = purchases.reduce((a, r) => a + r.purchasePrice, 0);
  const dayGaps = games.filter((r) => r.completed && /^\d{4}-/.test(String(r.datePurchased)) && /^\d{4}-/.test(String(r.dateCompleted)))
    .map((r) => (new Date(r.dateCompleted) - new Date(r.datePurchased)) / 864e5).filter((d) => d >= 0);
  const avgGapMo = dayGaps.length ? Math.round(dayGaps.reduce((a, b) => a + b, 0) / dayGaps.length / 30) : null;
  const ownedPhys = games.filter((r) => r.owned && (r.format || "").toLowerCase() === "physical");
  const valued = ownedPhys.map((r) => ({ r, v: collectionValueOf(r) })).filter((x) => x.v != null);
  const collectionVal = valued.reduce((a, x) => a + x.v, 0);
  const topValue = valued.sort((a, b) => b.v - a.v).slice(0, 10)
    .map((x) => ({ label: x.r.title, value: Math.round(x.v), link: gameLink(x.r, "games") }));
  let runSpend = 0;
  const cumSpend = [...spendMap.keys()].sort((a, b) => a - b).map((yy) => {
    runSpend += spendMap.get(yy);
    return { label: yr2(yy), value: Math.round(runSpend) };
  });
  const topValueRows = valued.slice(0, 12).map((x) => x.r);

  /* ---- ported from GamePicker's statistics/ selectors --------------------- */

  // Spend by quarter. A year is too coarse to see a Steam sale in.
  const qMap = new Map();
  for (const r of purchases) {
    const d = String(r.datePurchased);
    if (!/^\d{4}-\d{2}/.test(d)) continue;
    const q = `${d.slice(0, 4)} Q${Math.floor((+d.slice(5, 7) - 1) / 3) + 1}`;
    qMap.set(q, (qMap.get(q) || 0) + r.purchasePrice);
  }
  const quarterly = [...qMap.entries()].sort((a, b) => a[0].localeCompare(b[0])).slice(-16)
    .map(([q, v]) => ({ label: q.replace(" ", "\u2009"), value: Math.round(v) }));

  // How hard you actually played it: hours finished / days it took.
  const paced = rows.filter((r) => r.playTime > 0 && r.started && r.date && r.date >= r.started)
    .map((r) => {
      const days = Math.max(1, (new Date(r.date) - new Date(r.started)) / 864e5);
      return { r, perDay: r.playTime / days, days };
    });
  const bingeRows = paced.slice().sort((a, b) => b.perDay - a.perDay).slice(0, 10)
    .map((x) => ({ label: x.r.game, value: Math.round(x.perDay * 10) / 10, link: gameLink(x.r, "completed"),
                   tip: `${x.r.game}\n${fmtHours(x.r.playTime)} over ${Math.round(x.days)} day${x.days < 2 ? "" : "s"}` }));

  // How many games you had on the go at once. A sweep over start/finish events.
  const events = [];
  for (const r of rows) {
    if (!r.started || !r.date || r.date < r.started) continue;
    events.push([r.started, 1], [r.date, -1]);
  }
  events.sort((a, b) => String(a[0]).localeCompare(String(b[0])) || a[1] - b[1]);
  let cur = 0, peak = 0, peakOn = null;
  for (const [d, delta] of events) {
    cur += delta;
    if (cur > peak) { peak = cur; peakOn = d; }
  }

  // Buy it, then actually play it — how long does that take?
  const gapMonths = games
    .filter((r) => r.completed && /^\d{4}-/.test(String(r.datePurchased)) && /^\d{4}-/.test(String(r.dateCompleted)))
    .map((r) => (new Date(r.dateCompleted) - new Date(r.datePurchased)) / 864e5 / 30.4)
    .filter((m) => m >= 0);
  const gapBuckets = [
    ["Same month", (m) => m < 1], ["1-3 months", (m) => m >= 1 && m < 3],
    ["3-6 months", (m) => m >= 3 && m < 6], ["6-12 months", (m) => m >= 6 && m < 12],
    ["1-2 years", (m) => m >= 12 && m < 24], ["2-5 years", (m) => m >= 24 && m < 60],
    ["5 years+", (m) => m >= 60],
  ].map(([label, test]) => ({ label, value: gapMonths.filter(test).length }));
  // What a game is worth NOW minus what you paid for it. This is the one thing
  // the price data knows that the crown-jewels wall can't show: a $60 game worth
  // $58 is not a find, and a $5 one worth $180 is. Both ends, because the losses
  // are the more interesting half.
  const moved = ownedPhys
    .map((r) => ({ r, gain: (collectionValueOf(r) ?? NaN) - r.purchasePrice }))
    .filter((x) => r0(x.r.purchasePrice) && isFinite(x.gain) && Math.abs(x.gain) >= 1)
    .sort((a, b) => b.gain - a.gain);
  const moverCount = moved.length;
  const moverBar = (x) => ({
    label: x.r.title, value: Math.round(x.gain), link: gameLink(x.r, "games"),
    tip: `${x.r.title}\nPaid ${usd(x.r.purchasePrice)} · now ${usd(collectionValueOf(x.r))}\n${
      x.gain > 0 ? "Up" : "Down"} ${usd(Math.abs(x.gain))}`,
  });
  const movers = [...moved.slice(0, 8).map(moverBar), ...moved.slice(-5).reverse().map(moverBar)]
    .filter((b, i, a) => a.findIndex((o) => o.label === b.label) === i);   // tiny sets can overlap
  const topSales = games.map((r) => ({ r, v: salesOf(r) })).filter((x) => x.v != null)
    .sort((a, b) => b.v - a.v).slice(0, 10)
    .map((x) => ({ label: x.r.title, value: x.v, link: gameLink(x.r, "games") }));

  const sect = (title, panels) =>
    `<h2 class="stat-sec"><span>${escapeHtml(title)}</span><i>${panels.length}</i></h2>` +
    `<div class="stat-grid">${panels.join("")}</div>`;
  host.innerHTML =
    yearInReview(rows, games) +
    burnDown(rows, games) +
    `<h2 class="stat-sec">All time</h2>` +
    // Two ranks. Beaten / backlog / library-done are the three numbers the page is
    // actually about; the rest are supporting. Green means beaten and amber means
    // outstanding — the accent stays out of it.
    `<div class="stat-cards lead">
      ${statCard(rows.length, "Beaten", "", "", { tone: "good", icon: "i-trophy", sub: `of ${games.length.toLocaleString()} catalogued` })}
      ${statCard(backlog.length, "In backlog", "", "", { tone: "warn", icon: "i-clock", sub: `${Math.round(backlogHours).toLocaleString()} hours of it` })}
      ${statCard(complPct, "Library done", "", "%", { tone: "lead", icon: "i-target", sub: `${thisYear} beaten in ${curYear || "—"}` })}
    </div>
    <div class="stat-cards">
      ${statCard(Math.round(hours), "Hours played", "", "h", { icon: "i-clock" })}
      ${statCard(avg != null ? Math.round(avg * 100) : null, "Avg rating", "", "%", { icon: "i-star" })}
      ${statCard(avg != null && avgCrit != null ? `${Math.round(avg * 100)}/${Math.round(avgCrit * 100)}` : "—", "You vs critics", "", "", { icon: "i-trend" })}
      ${statCard(Math.round(totalSpent), "Total spent", "$", "", { icon: "i-package" })}
      ${statCard(Math.round(collectionVal), "Collection value", "$", "", { icon: "i-trend" })}
      ${statCard(avgGapMo != null ? avgGapMo : null, "Avg buy→finish", "", " mo", { icon: "i-calendar" })}
    </div>` +
    sect("Completed games", [
      statPanel("Finished vs added, cumulatively", multiLine([
        { points: cumAcquired, color: 3, name: "Acquired", label: `Acquired · ${last(cumAcquired)}` },
        { points: cumAdded, color: 1, name: "Added to the sheet", label: `Added to the sheet · ${last(cumAdded)}` },
        { points: cumulative, color: 0, name: "Finished", label: `Finished · ${rows.length.toLocaleString()}` },
      ]), "wide",
      "The gap between the lines is your backlog. Acquired uses Date Purchased; Added uses Date Added, which only starts in 2024 — that is when you began recording it, not a gap in the chart."),
      statPanel("Your hall of fame", posterRow(bestRows, { note: (r) => `${Math.round(r.rating * 100)}%` }), "wide"),
      statPanel("You vs the critics", scatter(scatterPts, { xLabel: "Critics", yLabel: "You" })),
      statPanel("Your taste, by genre", radar(genreRadar, { color: 4 }), "",
        "Your average rating per genre (0-100%), for genres you've finished 15+ games in. A long spoke means you rate that genre highly — not that you play it a lot."),
      statPanel("Completions per year", barsV(yearData, { tone: "good" }), "wide"),
      statPanel("Completions by month", barsV(monthData, { tone: "good" })),
      statPanel("By release decade", barsV(decadeData)),
      statPanel("Top platforms", barsH(countBars(rows, "platform", 10, "completed"))),
      statPanel("Top genres", barsH(countBars(rows, "genre", 12, "completed"))),
      statPanel("Top franchises", barsH(countBars(rows, "franchise", 10, "completed"))),
      statPanel("Top developers", barsH(countBars(rows, "developer", 10, "completed"))),
      statPanel("Top publishers", barsH(countBars(rows, "publisher", 10, "completed"))),
      statPanel("Rating distribution", barsH(ratingData)),
      statPanel("By region", barsH(countBars(rows, "region", 8, "completed"))),
      statPanel("How I played", barsH(flags)),
      statPanel("Longest playthroughs", barsH(longest, { fmt: (v) => v + "h" })),
      statPanel("Biggest me-vs-critic gaps", barsH(gaps, { fmt: (v) => (v > 0 ? "+" : "") + v, diverging: true }), "",
        "Green: you rated it higher than the critics did. Red: lower."),
      statPanel("Hardest you've binged", barsH(bingeRows, { fmt: (v) => v + "h/day" }), "",
        "Hours played divided by the days it took. The top of this list is a lost weekend."),
      statPanel("Games on the go at once", `<div class="s-big">
          <b>${peak}</b><span>at once, peaking ${peakOn ? escapeHtml(fmtDate(peakOn)) : "—"}</span>
        </div>`, "",
        "Counted from start and finish dates: how many playthroughs were open at the same time."),
    ]) +
    sect("Backlog", [
      statPanel("Backlog by platform", barsH(countBars(backlog, "platform", 10, "games"))),
      statPanel("Backlog by genre", barsH(countBars(backlog, "genre", 12, "games"))),
      statPanel("Backlog by length", barsH(backlogTime)),
      statPanel("Backlog by status", barsH(countBars(backlog, "playingStatus", 6, "games"))),
    ]) +
    sect("Purchases & collection", [
      statPanel("Spending per year", barsV(spendData, { fmt: usd, tone: "warn" }), "wide"),
      statPanel("Games bought per year", barsV(boughtData)),
      statPanel("Cumulative spend", areaLine(cumSpend, { color: 3, fmt: usd, label: usd(totalSpent) + " all in" }), "wide"),
      ...(VALUE_HISTORY && VALUE_HISTORY.length > 1
        ? [statPanel("Collection value over time",
            areaLine(VALUE_HISTORY.map((h) => ({ label: fmtDate(h.day).replace(/,.*/, ""), value: Math.round(h.total) })),
              { color: 2, fmt: usd, label: usd(VALUE_HISTORY[VALUE_HISTORY.length - 1].total) + " today" }), "wide")]
        : [statPanel("Collection value over time",
            `<div class="s-empty">Recording daily from today — a trend needs at least two points.
             ${VALUE_HISTORY && VALUE_HISTORY.length ? `First snapshot: ${escapeHtml(fmtDate(VALUE_HISTORY[0].day))} at ${usd(VALUE_HISTORY[0].total)}.` : ""}</div>`, "wide")]),
      statPanel("The crown jewels", posterRow(topValueRows, { note: (r) => usd(collectionValueOf(r)) }), "wide"),
      statPanel("Biggest movers", barsH(movers, { fmt: (v) => (v > 0 ? "+" : "-") + usd(Math.abs(v)) }), "wide",
        `What it's worth now minus what you paid, across ${moverCount.toLocaleString()} games where we know both. Up is profit.`),
      statPanel("Best selling (VGChartz)", barsH(topSales, { fmt: fmtUnits })),
      statPanel("Purchases by platform", barsH(countBars(purchases, "platform", 10, "games"))),
      statPanel("Spending by quarter", barsV(quarterly, { fmt: usd, tone: "warn" }), "wide",
        "A year is too coarse to see a Steam sale in."),
      statPanel("Bought, then finally played", barsH(gapBuckets), "",
        `How long a game waits between the till and the credits. ${gapMonths.length.toLocaleString()} games where we know both dates.`),
    ]) +
    predictionPanel();
  const yp = $("#yrPick");
  if (yp) yp.onchange = (e) => { statsState.year = +e.target.value; renderStats(); };
  host.querySelectorAll("[data-yg]").forEach((el) => {
    el.onclick = () => {
      const row = rows.find((r) => String(r._k || "") === el.dataset.yg);
      if (row) openDrawer(row, "completed");
    };
  });
  wireCharts(host);
}

// ---- "Pick my next game" ------------------------------------------------
const pickState = { selector: "backlog", param: "", picked: null, minutes: 0 };
let _completedFranchises = null;
const completedFranchises = () => (_completedFranchises ||=
  new Set(((DATA.sheets.completed || {}).rows || []).map((r) => r.franchise).filter(Boolean)));
const pickYear = () => new Date().getFullYear();

const quickF = (r) => { const p = playtimeOf(r); return p != null && p < 5; };
const acclaimedF = (r) => { const m = metacriticOf(r); return m != null && m >= 0.8; };
const retroF = (r) => { const y = +r.releaseYear; return y && y < 2000; };
const modeIncludes = (r, m) => { const e = ENRICH[r._k]; return !!(e && e.gameModes && e.gameModes.some((x) => x.toLowerCase().includes(m))); };

// Each selector filters the backlog (games not completed). `param` selectors
// take a value (platform/genre/…); `topBy` narrows to extremes before the roll.
// Curated + expanded from zdiemer/GamePicker's selector library.
/* ---- the fun ones ---------------------------------------------------------
   Ported from zdiemer/GamePicker's characteristics/ selectors. These exist because
   sometimes you don't want a good game, you want an EXCUSE — a reason to play this
   one rather than that one. */

// The month/day you were born. Read from the sheet's own data would be nice, but
// there's nowhere to put it — so it's a setting, remembered per browser.
const BIRTHDAY_KEY = "gamedex.birthday";
const birthday = () => localStorage.getItem(BIRTHDAY_KEY) || "";     // "MM-DD"

/* Pick a game by the colour of its box.

   The cover's dominant colour is already computed for the drawer's accent
   (extras.js coverAccent: a tiny canvas, near-black and near-white pixels thrown
   away, the colourful ones weighted up). So this needs no new machinery — just a
   hue bucket and somewhere to put the answer. Covers resolve asynchronously, so
   the pool fills in as the art loads; that's why the count climbs while you look
   at it. */
const HUE_BUCKETS = [
  { id: "red", label: "Red", test: (h, s) => s > .18 && (h < 18 || h >= 342) },
  { id: "orange", label: "Orange", test: (h, s) => s > .18 && h >= 18 && h < 45 },
  { id: "yellow", label: "Yellow", test: (h, s) => s > .18 && h >= 45 && h < 68 },
  { id: "green", label: "Green", test: (h, s) => s > .18 && h >= 68 && h < 160 },
  { id: "blue", label: "Blue", test: (h, s) => s > .18 && h >= 160 && h < 255 },
  { id: "purple", label: "Purple", test: (h, s) => s > .18 && h >= 255 && h < 300 },
  { id: "pink", label: "Pink", test: (h, s) => s > .18 && h >= 300 && h < 342 },
  { id: "mono", label: "Black, white or grey", test: (h, s) => s <= .18 },
];

const COVER_HUE = new Map();     // matchKey -> bucket id
function coverHueOf(row) {
  if (COVER_HUE.has(row._k)) return COVER_HUE.get(row._k);
  const src = coverSrc(ENRICH[row._k], "cover_small");
  if (!src) return null;
  coverAccent(src, (accent) => {
    const m = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(accent) ||
              /#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(accent);
    if (!m) return;
    const base = accent.startsWith("#") ? 16 : 10;
    const [r, g, b] = [parseInt(m[1], base), parseInt(m[2], base), parseInt(m[3], base)];
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    const sat = max ? d / max : 0;
    let h = 0;
    if (d) {
      if (max === r) h = 60 * (((g - b) / d) % 6);
      else if (max === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
    }
    if (h < 0) h += 360;
    const hit = HUE_BUCKETS.find((x) => x.test(h, sat));
    COVER_HUE.set(row._k, hit ? hit.id : null);
  });
  return COVER_HUE.get(row._k) ?? null;
}

const alphaOnly = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const isPalindrome = (t) => {
  const a = alphaOnly(t);
  return a.length >= 5 && a === [...a].reverse().join("");
};
// "Obscure" = nobody has rated it anywhere. Not bad, not unknown to YOU — unknown
// to everyone, which is a different and more interesting thing.
const isObscure = (r) => {
  const e = ENRICH[r._k] || {};
  return metacriticOf(r) == null && userRatingOf(r) == null && salesOf(r) == null && !e.igdbId;
};

const SELECTORS = [
  { id: "backlog", label: "Anything in my backlog", group: "General", filter: () => true },
  { id: "neverstarted", label: "Never started", group: "General", filter: (r) => r.owned && !r.dateStarted && !r.playingStatus },
  { id: "unfinished", label: "Started but unfinished", group: "General", filter: (r) => r.dateStarted && !r.completed },
  { id: "recentadd", label: "Recently added", group: "General", filter: (r) => !!r.dateAdded, topBy: { by: (r) => r.dateAdded, desc: true, take: 150 } },
  { id: "aging", label: "Longest in my backlog", group: "General", filter: (r) => !!(r.datePurchased || r.dateAdded), topBy: { by: (r) => r.datePurchased || r.dateAdded, desc: false, take: 150 } },

  { id: "playing", label: "Currently playing", group: "Status", filter: (r) => r.playingStatus === "Playing" },
  { id: "upnext", label: "Up next", group: "Status", filter: (r) => r.playingStatus === "Up Next" },
  { id: "onhold", label: "On hold", group: "Status", filter: (r) => r.playingStatus === "On Hold" },
  { id: "priority", label: "High priority", group: "Status", filter: (r) => priorityRank(r.priority) >= 4 },

  // ---- for the hell of it ----
  { id: "birthday", label: "Released on my birthday", group: "For the hell of it",
    filter: (r) => {
      const b = birthday();
      return !!b && typeof r.releaseDate === "string" && r.releaseDate.slice(5, 10) === b;
    } },
  { id: "palindrome", label: "Palindrome titles", group: "For the hell of it",
    filter: (r) => isPalindrome(r.title) },
  { id: "longtitle", label: "Absurdly long titles", group: "For the hell of it",
    filter: (r) => String(r.title || "").length > 45,
    topBy: { by: (r) => String(r.title).length, desc: true, take: 150 } },
  { id: "shorttitle", label: "One-word titles", group: "For the hell of it",
    filter: (r) => String(r.title || "").trim().split(/\s+/).length === 1 },
  { id: "obscure", label: "Nobody has heard of these", group: "For the hell of it",
    filter: isObscure },
  { id: "cooptimus", label: "Verified co-op (Co-Optimus)", group: "For the hell of it",
    filter: (r) => { const e = ENRICH[r._k] || {}; return e.coopLocal > 1 || e.coopOnline > 1; } },
  { id: "couch", label: "Co-op on one couch", group: "For the hell of it",
    filter: (r) => (ENRICH[r._k] || {}).coopLocal > 1 },
  { id: "colour", label: "By the colour of the box", group: "For the hell of it",
    param: "__hue",
    paramVals: () => HUE_BUCKETS.map((b) => b.label),
    filter: (r, p) => {
      const want = HUE_BUCKETS.find((b) => b.label === p);
      return !!want && coverHueOf(r) === want.id;
    } },
  { id: "maxpriority", label: "Top priority", group: "Status", filter: (r) => priorityRank(r.priority) >= 5 },

  { id: "onesit", label: "One sitting (under 2h)", group: "Playtime", filter: (r) => { const p = playtimeOf(r); return p != null && p < 2; } },
  { id: "quick", label: "Quick (under 5h)", group: "Playtime", filter: quickF },
  { id: "medium", label: "Medium (5–15h)", group: "Playtime", filter: (r) => { const p = playtimeOf(r); return p != null && p >= 5 && p < 15; } },
  { id: "long", label: "Long haul (15–40h)", group: "Playtime", filter: (r) => { const p = playtimeOf(r); return p != null && p >= 15 && p < 40; } },
  { id: "marathon", label: "Marathon (40h+)", group: "Playtime", filter: (r) => { const p = playtimeOf(r); return p != null && p >= 40; } },

  { id: "acclaimed", label: "Critically acclaimed (80+)", group: "Rating", filter: acclaimedF },
  { id: "masterpiece", label: "Masterpieces (90+)", group: "Rating", filter: (r) => { const m = metacriticOf(r); return m != null && m >= 0.9; } },
  { id: "beloved", label: "Beloved by players (80+)", group: "Rating", filter: (r) => { const m = userRatingOf(r); return m != null && m >= 0.8; } },
  { id: "shortsweet", label: "Short & sweet (< 5h, 80+)", group: "Rating", filter: (r) => quickF(r) && acclaimedF(r) },
  { id: "retrogem", label: "Retro gems (pre-2000, 80+)", group: "Rating", filter: (r) => retroF(r) && acclaimedF(r) },

  { id: "owned", label: "Owned & unplayed", group: "Ownership & price", filter: (r) => !!r.owned },
  { id: "physical", label: "Physical copies", group: "Ownership & price", filter: (r) => r.owned && (r.format || "").toLowerCase() === "physical" },
  { id: "digital", label: "Digital copies", group: "Ownership & price", filter: (r) => r.owned && (r.format || "").toLowerCase() === "digital" },
  { id: "wishlist", label: "Wishlisted", group: "Ownership & price", filter: (r) => !!r.wishlisted },
  { id: "free", label: "Free games", group: "Ownership & price", filter: (r) => r.owned && r.purchasePrice === 0 },
  { id: "cheap", label: "Cheap (under $10)", group: "Ownership & price", filter: (r) => r.purchasePrice != null && r.purchasePrice > 0 && r.purchasePrice < 10 },
  { id: "unplayedbuy", label: "Unplayed purchases", group: "Ownership & price", filter: (r) => r.owned && r.purchasePrice != null },

  { id: "coopmodes", label: "Co-op (per IGDB)", group: "Play style", filter: (r) => modeIncludes(r, "co-op") || modeIncludes(r, "cooperative") },
  { id: "multi", label: "Multiplayer", group: "Play style", filter: (r) => modeIncludes(r, "multiplayer") },
  { id: "solo", label: "Single-player", group: "Play style", filter: (r) => modeIncludes(r, "single player") },
  { id: "vr", label: "VR games", group: "Play style", filter: (r) => !!r.vr },
  { id: "dlc", label: "DLC & expansions", group: "Play style", filter: (r) => !!r.dlc },

  { id: "retro", label: "Retro (before 2000)", group: "Era", filter: retroF },
  { id: "recent", label: "Recent (last 3 years)", group: "Era", filter: (r) => { const y = +r.releaseYear; return y && y >= pickYear() - 3; } },
  { id: "thisyear", label: "This year's releases", group: "Era", filter: (r) => +r.releaseYear === pickYear() },

  { id: "franchise", label: "Continue a franchise I've played", group: "Progress", filter: (r) => r.franchise && completedFranchises().has(r.franchise) },

  { id: "platform", label: "By platform…", group: "By…", param: "platform", filter: (r, v) => r.platform === v },
  { id: "genre", label: "By genre…", group: "By…", param: "genre", filter: (r, v) => r.genre === v },
  { id: "byfranchise", label: "By franchise…", group: "By…", param: "franchise", filter: (r, v) => r.franchise === v },
  { id: "bydev", label: "By developer…", group: "By…", param: "developer", filter: (r, v) => r.developer === v },
  { id: "bypub", label: "By publisher…", group: "By…", param: "publisher", filter: (r, v) => r.publisher === v },
];

const pickEligible = () => ((DATA.sheets.games || {}).rows || []).filter((r) => !r.completed && r.title);

// "I have 45 minutes." A game qualifies if you could plausibly FINISH it in the
// time you've got — HLTB's main-story number where we have it, the sheet's
// estimate otherwise. 0 means "don't care".
function withinTimeBudget(rows) {
  if (!pickState.minutes) return rows;
  const hours = pickState.minutes / 60;
  return rows.filter((r) => { const t = playtimeOf(r); return t != null && t <= hours; });
}
function pickPool() {
  const sel = SELECTORS.find((s) => s.id === pickState.selector) || SELECTORS[0];
  let pool = withinTimeBudget(
    pickEligible().filter((r) => (sel.param ? pickState.param && sel.filter(r, pickState.param) : sel.filter(r))));
  if (sel.topBy && pool.length) {   // narrow to extremes (recent/oldest) before rolling
    pool = [...pool].sort((a, b) => { const x = sel.topBy.by(a) || "", y = sel.topBy.by(b) || ""; return x < y ? -1 : x > y ? 1 : 0; });
    if (sel.topBy.desc) pool.reverse();
    pool = pool.slice(0, sel.topBy.take);
  }
  return { sel, pool };
}
function pickGame() {
  const { pool } = pickPool();
  pickState.picked = pool.length ? pool[Math.floor(Math.random() * pool.length)] : null;
  renderPicker();
}

function pickCard(row) {
  const cs = coverSrc(ENRICH[row._k], "cover_big");
  const cover = cs ? `<img src="${cs}" alt="">` : `<div class="pick-ph">${icon("i-library", 30)}</div>`;
  const pt = playtimeOf(row), mc = metacriticOf(row);
  const bits = [row.platform, row.releaseYear, row.genre].filter((x) => x != null && x !== "").map((x) => escapeHtml(String(x)));
  if (pt != null) bits.push("⏱ " + fmtHours(pt));
  if (mc != null) bits.push("★ " + Math.round(mc * 100));
  return `<div class="pick-card">${cover}<div class="pick-info"><h2>${escapeHtml(String(row.title))}</h2>
    <div class="pick-meta">${bits.join(" · ")}</div>
    <div class="pick-actions"><button class="pick-reroll" id="pickReroll">${icon("i-dice", 15)} Re-roll</button>
    <span class="muted">Tap the card for full details</span></div></div></div>`;
}

function renderPicker() {
  const host = $("#picker");
  const sel = SELECTORS.find((s) => s.id === pickState.selector) || SELECTORS[0];
  pickState.selector = sel.id;
  const groups = {};
  SELECTORS.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });
  const opts = Object.entries(groups).map(([g, ss]) =>
    `<optgroup label="${g}">${ss.map((s) => `<option value="${s.id}" ${s.id === sel.id ? "selected" : ""}>${escapeHtml(s.label)}</option>`).join("")}</optgroup>`).join("");
  let paramHtml = "";
  // The birthday selector needs a birthday. There's nowhere in the sheet to keep
  // one, so it lives in this browser — set it once.
  if (sel.id === "birthday") {
    const b = birthday();
    paramHtml = `<input id="pickBday" type="date" value="${b ? `2000-${b}` : ""}"
      title="Only the month and day are used" style="max-width:170px">`;
  }
  if (sel.param) {
    // A param is usually a row FIELD (platform, genre). Some are computed — the
    // colour of the box isn't in the sheet — so a selector can supply its own.
    const vals = sel.paramVals
      ? sel.paramVals()
      // Rank values by how many backlog games each has (keeps big lists usable).
      : topCounts(pickEligible().map((r) => r[sel.param]), 200).map((x) => `${x.label}`);
    if (!vals.includes(pickState.param)) pickState.param = vals[0] || "";
    paramHtml = `<select id="pickParam">${vals.map((v) => `<option ${v === pickState.param ? "selected" : ""}>${escapeHtml(v)}</option>`).join("")}</select>`;
  }
  const { pool } = pickPool();
  const TIMES = [[0, "Any length"], [30, "30 minutes"], [45, "45 minutes"], [60, "1 hour"],
                 [120, "2 hours"], [300, "5 hours"], [600, "10 hours"]];
  host.innerHTML = `
    <div class="pick-controls">
      <label>Pick <select id="pickSel">${opts}</select></label>${paramHtml}
      <label class="pick-time">I have
        <select id="pickTime">${TIMES.map(([m, l]) =>
          `<option value="${m}" ${m === pickState.minutes ? "selected" : ""}>${escapeHtml(l)}</option>`).join("")}</select>
      </label>
      <button id="pickBtn" class="pick-btn">${icon("i-dice", 16)} Pick for me</button>
      <span class="pick-count">${pool.length.toLocaleString()} game${pool.length === 1 ? "" : "s"} in pool</span>
    </div>
    <div class="pick-result" id="pickResult">${pickState.picked && pool.includes(pickState.picked)
      ? pickCard(pickState.picked)
      : `<div class="pick-empty">${pool.length ? "Hit “Pick for me” to roll a game." : "No backlog games match this selector."}</div>`}</div>`;
  $("#pickSel").onchange = (e) => { pickState.selector = e.target.value; pickState.param = ""; pickState.picked = null; renderPicker(); nav(); };
  const pp = $("#pickParam");
  if (pp) pp.onchange = (e) => { pickState.param = e.target.value; pickState.picked = null; renderPicker(); nav(); };
  $("#pickTime").onchange = (e) => { pickState.minutes = +e.target.value; pickState.picked = null; renderPicker(); nav(); };
  const bd = $("#pickBday");
  if (bd) bd.onchange = (e) => {
    const v = e.target.value;                       // yyyy-mm-dd; only MM-DD is used
    if (v) localStorage.setItem(BIRTHDAY_KEY, v.slice(5, 10));
    else localStorage.removeItem(BIRTHDAY_KEY);
    pickState.picked = null;
    renderPicker();
  };

  $("#pickBtn").onclick = () => { pickGame(); nav(); };
  const card = host.querySelector(".pick-card");
  if (card) {
    card.onclick = () => openDrawer(pickState.picked, "games");
    $("#pickReroll").onclick = (e) => { e.stopPropagation(); pickGame(); nav(); };
  }
}

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  const st = tabState[activeTab];
  if (!st) return;             // a special tab (Home, Stats…) has no results list to filter
  st.search = e.target.value;
  st.page = 1;
  // Coalesce keystrokes. Filtering 14.7k rows and rebuilding every facet is
  // fast now, but not fast enough to do it between two quick keypresses.
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    renderAll();
    syncURL(false);        // replace so typing doesn't flood history
  }, 140);
});
// Enter from a tab with no results list of its own (Home, Stats, Shelf, …) takes the
// query to All Games and shows it there, which is what a search box implies.
$("#search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  if (["games", "completed", "onOrder"].includes(activeTab)) { e.target.blur(); return; }
  const q = e.target.value;
  tabState.games.search = q;
  tabState.games.page = 1;
  switchTab("games");      // no reset — keep the query we just set
  nav();
});
// closest(), not e.target: a tab now contains an <svg> and a <span>, so the click
// lands on the icon and e.target.dataset.tab is undefined. This is exactly what
// broke navigation when the emoji became icons.
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-tab]");
  if (btn) { switchTab(btn.dataset.tab, true); nav(); }
});
$("#clear").addEventListener("click", () => {
  const st = tabState[activeTab];
  st.search = ""; st.facets = {}; st.page = 1;
  $("#search").value = "";
  renderAll();
  nav();
});
$("#resetsort").addEventListener("click", () => {
  tabState[activeTab].sort = null;
  tabState[activeTab].page = 1;
  renderAll();
  nav();
});
$("#pagesize").addEventListener("change", (e) => {
  PAGE_SIZE = parseInt(e.target.value, 10) || 50;
  tabState[activeTab].page = 1;
  renderTable(currentFiltered);
  nav();
});
function setView(mode) {
  tabState[activeTab].view = mode;      // renderTable paints the active state
  renderTable(currentFiltered);
}
$("#viewTable").addEventListener("click", () => { setView("table"); nav(); });
$("#viewGrid").addEventListener("click", () => { setView("grid"); nav(); });
$("#viewTimeline").addEventListener("click", () => { setView("timeline"); nav(); });
$("#combine").addEventListener("click", () => {
  const st = tabState[activeTab];
  st.combine = !st.combine;
  st.page = 1;                          // the row count just changed under us
  renderTable(currentFiltered);
  nav();
});
// ---- Mobile floating controls ------------------------------------------
// On mobile the page is one scroller, so the result bar scrolls away. Move the
// sort/per-page/view cluster into a bottom sheet and reach it from a FAB.
const MOBILE = window.matchMedia("(max-width: 760px)");
function placeControls() {
  const ctrls = $("#rbControls");
  const home = MOBILE.matches ? $("#sheetBody") : $(".resultbar");
  if (ctrls.parentElement !== home) home.appendChild(ctrls);
  if (!MOBILE.matches) setSheet(false);
}
function setSheet(open) {
  $("#sheet").hidden = !open;
  $("#sheetBackdrop").hidden = !open;
}
MOBILE.addEventListener("change", placeControls);
placeControls();

$("#fabFilters").addEventListener("click", () => setFacets(true));
$("#fabSort").addEventListener("click", () => setSheet(true));
$("#fabTop").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
$("#sheetClose").addEventListener("click", () => setSheet(false));
$("#sheetBackdrop").addEventListener("click", () => setSheet(false));
// Picking a sort/view is the whole point of the sheet — dismiss it so the
// results are visible immediately. (The controls' own handlers still run.)
$("#sheetBody").addEventListener("change", () => setSheet(false));
$("#sheetBody").addEventListener("click", (e) => {
  if (e.target.closest(".viewtoggle, .dirbtn")) setSheet(false);
});

// ---- theme ---------------------------------------------------------------
// An explicit choice wins and persists; otherwise follow the OS.
const THEME_KEY = "gamedex.theme";
// data-theme is ALWAYS set explicitly. Leaving it off means "dark" to the CSS
// but "whatever the OS says" to JS, and the two disagree the moment the OS
// prefers light — the toggle then computes light→dark and appears to do nothing.
function currentTheme() {
  return localStorage.getItem(THEME_KEY)
    || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  // Show the mode you'd switch TO, so the control says what it does.
  $("#theme").innerHTML = icon(t === "dark" ? "i-sun" : "i-moon", 16);
  $("#theme").title = t === "dark" ? "Switch to light" : "Switch to dark";
}
applyTheme(currentTheme());
// The shortcut differs by platform, so the keycap should too.
{
  const mod = $("#cmdkMod");
  if (mod && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)) mod.textContent = "\u2318";
}
$("#theme").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
  if (activeTab === "stats") renderStats();     // recolour the charts' text
});

// ---- command palette (⌘K / Ctrl-K) ---------------------------------------
// 14.7k games is too many to browse to. Type a few letters, hit enter.
const cmdk = { open: false, sel: 0, results: [] };

// Read the tab list from the live header, so the palette can never go stale — adding
// or removing a tab (Shelf in, Reviews out) updates it for free.
function cmdkTabs() {
  return [...document.querySelectorAll("#tabs button[data-tab]")].map((b) => ({
    id: b.dataset.tab,
    label: (b.querySelector("span") || {}).textContent || b.dataset.tab,
    icon: ((b.querySelector("use") || {}).getAttribute?.("href") || "#i-home").slice(1),
  }));
}

function cmdkCandidates(q) {
  const out = [];
  const needle = q.toLowerCase().trim();
  if (!needle) {
    return cmdkTabs().map((t) => ({ kind: "Tab", label: t.label, icon: t.icon, run: () => switchTab(t.id) }));
  }
  // Tabs
  for (const t of cmdkTabs()) {
    if (t.label.toLowerCase().includes(needle))
      out.push({ kind: "Tab", label: t.label, icon: t.icon, run: () => switchTab(t.id) });
  }
  // Games — prefix matches first, then substring. Capped, so typing stays fast.
  const rows = (DATA.sheets.games || {}).rows || [];
  const pre = [], sub = [];
  for (const r of rows) {
    const t = String(r.title || "").toLowerCase();
    if (!t) continue;
    const i = t.indexOf(needle);
    if (i === 0) pre.push(r);
    else if (i > 0) sub.push(r);
    if (pre.length >= 30) break;
  }
  for (const r of [...pre, ...sub].slice(0, 24)) {
    out.push({
      kind: "Game", label: String(r.title), sub: [r.platform, r.releaseYear].filter(Boolean).join(" · "),
      row: r, run: () => { switchTab("games"); openDrawer(r, "games"); },
    });
  }
  // Facet values on the current tab (platform / genre / franchise / …)
  for (const f of cmdkFacetIndex()) {
    if (out.length > 40) break;
    if (!f.lower.includes(needle)) continue;
    out.push({
      kind: f.label, label: f.val,
      run: () => {
        const st = tabState[activeTab];
        st.facets[f.key] = new Set([f.val]);
        st.page = 1;
        renderAll(); nav();
      },
    });
  }
  return out.slice(0, 40);
}

// Distinct facet values for the active tab, computed once. Scanning 14.7k rows
// on every keystroke would make the palette crawl.
const _cmdkFacets = {};
function cmdkFacetIndex() {
  if (_cmdkFacets[activeTab]) return _cmdkFacets[activeTab];
  // Special tabs (Home, Stats, Shelf…) have no sheet, so columns()/facetCols() throw —
  // which is exactly why the palette worked on All Games and Completed but broke the
  // moment you typed anywhere else. No sheet, no facet values.
  if (!DATA.sheets[activeTab]) return (_cmdkFacets[activeTab] = []);
  const out = [];
  const rows = (sheet() || { rows: [] }).rows || [];
  for (const col of facetCols()) {
    if (col.virtual) continue;
    const seen = new Set();
    for (const row of rows) {
      for (const it of rowFacetItems(row, col)) {
        if (seen.has(it.key)) continue;
        seen.add(it.key);
        out.push({ key: col.key, label: col.label, val: String(it.key), lower: String(it.key).toLowerCase() });
      }
    }
  }
  return (_cmdkFacets[activeTab] = out);
}

function cmdkRender() {
  const host = $("#cmdkResults");
  if (!cmdk.results.length) {
    host.innerHTML = `<div class="cmdk-none">No matches</div>`;
    return;
  }
  host.innerHTML = cmdk.results.map((r, i) => {
    const cover = r.row ? coverSrc(ENRICH[r.row._k], "cover_small") : "";
    const art = r.row
      ? (cover ? `<img src="${escapeHtml(cover)}" alt="">` : `<span class="cmdk-ph">${icon("i-library", 15)}</span>`)
      : "";
    return `<button class="cmdk-item${i === cmdk.sel ? " sel" : ""}" data-i="${i}">
      ${art}<span class="cmdk-txt"><b>${escapeHtml(r.label)}</b>${r.sub ? `<span>${escapeHtml(r.sub)}</span>` : ""}</span>
      <span class="cmdk-kind">${escapeHtml(r.kind)}</span></button>`;
  }).join("");
  const sel = host.querySelector(".cmdk-item.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
  host.querySelectorAll(".cmdk-item").forEach((el) => {
    el.onclick = () => cmdkRun(+el.dataset.i);
  });
}
function cmdkSearch() {
  cmdk.results = DATA ? cmdkCandidates($("#cmdkInput").value) : [];
  cmdk.sel = 0;
  cmdkRender();
}
function cmdkRun(i) {
  const r = cmdk.results[i];
  setCmdk(false);
  if (r) r.run();
}
function setCmdk(open) {
  cmdk.open = open;
  $("#cmdkOverlay").hidden = !open;
  if (open) {
    $("#cmdkInput").value = "";
    cmdkSearch();
    $("#cmdkInput").focus();
  }
  syncScrollLock();
}
$("#cmdk").addEventListener("click", () => setCmdk(true));
$("#cmdkOverlay").addEventListener("click", (e) => { if (e.target === $("#cmdkOverlay")) setCmdk(false); });
$("#cmdkInput").addEventListener("input", cmdkSearch);
$("#cmdkInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); cmdk.sel = Math.min(cmdk.results.length - 1, cmdk.sel + 1); cmdkRender(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); cmdk.sel = Math.max(0, cmdk.sel - 1); cmdkRender(); }
  else if (e.key === "Enter") { e.preventDefault(); cmdkRun(cmdk.sel); }
});

// Wordmark = home: back to the landing page with nothing filtered/sorted.
$("#brand").addEventListener("click", () => {
  for (const t of TABS) tabState[t] = { ...freshState(), view: tabState[t].view, combine: tabState[t].combine };
  pickState.selector = "backlog"; pickState.param = ""; pickState.picked = null;
  $("#search").value = "";
  setFacets(false);
  switchTab("home");
  nav();
});
$("#facetToggle").addEventListener("click", () => setFacets(!$("#facets").classList.contains("open")));
$("#facetBackdrop").addEventListener("click", () => setFacets(false));
$("#gridsort").addEventListener("change", (e) => {
  const st = tabState[activeTab];
  const k = e.target.value;
  if (k === "__default" || (activeTab === "games" && k === "releaseDate")) {
    st.sort = null;          // the default: releaseDateDesc, which ranks "Early Access" newest
  } else {
    const c = sortMeta(k);
    st.sort = [{ key: k, dir: c && c.type === "text" ? "asc" : "desc",
                 type: c && c.type, kind: c && c.kind }];
  }
  st.page = 1;
  renderTable(currentFiltered);
  nav();
});
$("#gridsortdir").addEventListener("click", () => {
  const st = tabState[activeTab];
  if (st.sort && st.sort.length) {
    st.sort[0].dir = st.sort[0].dir === "asc" ? "desc" : "asc";
    st.page = 1;
    renderTable(currentFiltered);
    nav();
  }
});
$("#drawerBack").addEventListener("click", drawerBack);
$("#drawerClose").addEventListener("click", closeDrawer);
$("#overlay").addEventListener("click", (e) => { if (e.target.id === "overlay") closeDrawer(); });
$("#drawerBody").addEventListener("click", (e) => {
  const a = e.target.closest(".facet-link");
  if (!a) return;
  e.preventDefault(); e.stopPropagation();
  applyDrawerFacet(a.dataset.fk, a.dataset.fv);
});
$("#lbClose").addEventListener("click", closeLightbox);
$("#lbPrev").addEventListener("click", (e) => { e.stopPropagation(); lbShow(-1); });
$("#lbNext").addEventListener("click", (e) => { e.stopPropagation(); lbShow(1); });
$("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") closeLightbox(); });
document.addEventListener("keydown", (e) => {
  if (lightboxOpen()) {                       // lightbox owns the keys while open
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowLeft") lbShow(-1);
    else if (e.key === "ArrowRight") lbShow(1);
    return;
  }
  if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    setCmdk(!cmdk.open);
    return;
  }
  if (e.key !== "Escape") return;
  if (cmdk.open) setCmdk(false);
  else if (!$("#sheet").hidden) setSheet(false);
  // Esc unwinds the drawer history one step at a time, then closes.
  else if (drawerStack.length && !$("#overlay").hidden) drawerBack();
  else closeDrawer();
});

function showToast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { t.classList.remove("show"); setTimeout(() => (t.hidden = true), 250); }, 2500);
}

$("#refresh").addEventListener("click", async () => {
  const btn = $("#refresh");
  btn.classList.add("spinning"); btn.disabled = true;
  try {
    const res = await fetch("api/refresh", { method: "POST" });
    const j = await res.json();
    if (res.ok) {
      const dres = await fetch("api/data", { cache: "no-store" });
      if (dres.ok) {
        DATA = await dres.json();
        resetCollections();
        resetSearchCache();
        const en = DATA.meta && DATA.meta.enrichment;
        ENRICH_ENABLED = !!(en && en.enabled !== false);
        setFreshness(); renderAll(); loadAllEnrichment();
      }
      showToast(j.changed ? "Spreadsheet updated ✓" : "Already up to date");
    } else showToast("Refresh failed: " + (j.error || res.status));
  } catch (_) { showToast("Refresh failed"); }
  finally { btn.classList.remove("spinning"); btn.disabled = false; }
});

load().catch((err) => { console.error(err); $("#count").textContent = "Error: " + err.message; });
