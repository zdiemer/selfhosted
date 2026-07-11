"use strict";

// ---- config -------------------------------------------------------------
let PAGE_SIZE = 50;
let viewMode = "grid";             // "table" | "grid"
const FACET_CAP = 12;              // values shown before "show more"
const FACET_FILTER_THRESHOLD = 12; // show a per-facet search box past this many values

// ---- state --------------------------------------------------------------
let DATA = null;            // {meta, sheets}
let activeTab = "home";
const TABS = ["games", "completed", "onOrder"];
// Per-tab UI state, isolated so switching tabs preserves filters.
const tabState = {};
for (const t of TABS) {
  tabState[t] = { search: "", facets: {}, expanded: {}, sort: null, page: 1 };
}

const $ = (sel) => document.querySelector(sel);

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
const ENRICH_REQUESTED = new Set();
let enrichTimer = null;
let drawerRow = null;              // row currently shown in the drawer (for sheet fallback)

// A cover is "pending" while enrichment is still resolving and we've not seen it.
const coverPending = (row) => ENRICH_ENABLED && !ENRICH_COMPLETE && !(row._k in ENRICH);
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
    if (changed) patchEnrichedCells();                      // in-place: no flicker
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
  if (d.developers && d.developers.length) meta.push(`<div class="detail-row"><div class="k">Developer</div><div class="v">${linkList(d.developers, "developer")}</div></div>`);
  if (d.publishers && d.publishers.length) meta.push(`<div class="detail-row"><div class="k">Publisher</div><div class="v">${linkList(d.publishers, "publisher")}</div></div>`);
  const nShots = mediaOf(d).length;
  const shots = nShots
    ? `<div class="shots"><div class="shot-view"></div>` +
      (nShots > 1 ? `<button class="shot-nav prev" aria-label="Previous">‹</button><button class="shot-nav next" aria-label="Next">›</button>` : "") +
      `<div class="shot-count"></div><div class="shot-cap"></div></div>` : "";
  const similar = (d.similar || []).filter((s) => s.cover).slice(0, 8);
  const simHtml = similar.length
    ? `<div class="detail-row notes"><div class="k">Similar games</div><div class="similar">${similar.map((s) =>
        `<a href="${escapeHtml(s.url || "#")}" target="_blank" rel="noopener" title="${escapeHtml(s.name)}"><img loading="lazy" src="${IMG(s.cover, "cover_small")}" alt=""><span>${escapeHtml(s.name)}</span></a>`).join("")}</div></div>` : "";
  const text = d.summary || d.storyline;
  // The cover, score and chips live in the hero now — this is just the prose.
  return (badge ? `<div class="badges">${badge}</div>` : "") +
    (text ? `<div class="detail-row notes"><div class="k">Summary (IGDB)</div><div class="v">${escapeHtml(text)}</div></div>` : "") +
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
  if (!cells.length) return "";
  return `<div class="hero-stats">` + cells.slice(0, 6).map(([v, l, cls]) =>
    `<div class="hero-stat"><b class="${cls}">${escapeHtml(String(v))}</b><span>${escapeHtml(l)}</span></div>`).join("") + `</div>`;
}

function heroHtml(row, titleText) {
  const cs = coverSrc(ENRICH[row._k], "cover_big");
  const pixel = coverIsPixelArt(ENRICH[row._k], cs) ? " pixel" : "";
  const cover = cs
    ? `<img class="cover-big${pixel}" id="heroCover" src="${escapeHtml(cs)}" alt="">`
    : `<div class="cover-big skel" id="heroCover"></div>`;
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
  if (ENRICH_SOURCES.includes("guides")) rows.push({ id: "guides", label: "StrategyWiki guide", ph: "strategywiki.org/wiki/<Page>" });
  return `<details class="map-menu"><summary>🔧 Fix mapping</summary>` +
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
      const frame = document.createElement("iframe");
      frame.className = "shot-video";
      // muted: a browser will refuse to autoplay with sound, and it would be
      // rude anyway. Controls are on, so it can be unmuted.
      frame.src = `https://www.youtube-nocookie.com/embed/${m.id}?autoplay=1&mute=1&rel=0&modestbranding=1&playsinline=1`;
      frame.allow = "accelerometer; autoplay; encrypted-media; picture-in-picture";
      frame.allowFullscreen = true;
      frame.title = m.name;
      view.appendChild(frame);
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
    cap.textContent = m.kind === "video" ? m.name : (m.art ? "Artwork" : "");
  };

  const prev = wrap.querySelector(".prev"), next = wrap.querySelector(".next");
  if (prev) prev.onclick = (e) => { e.stopPropagation(); show(shotIdx - 1); };
  if (next) next.onclick = (e) => { e.stopPropagation(); show(shotIdx + 1); };
  show(0);
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
function openLightbox(i) { lbIdx = i; $("#lightbox").hidden = false; lbShow(0); }
function closeLightbox() { $("#lightbox").hidden = true; }
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
  return `<div class="hltb"><div class="hltb-head">💵 Value (GameEye)</div>` +
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
  return `<div class="hltb"><div class="hltb-head">🕹️ Arcade cabinet${a.romset ? ` <span class="muted">${escapeHtml(a.romset)}</span>` : ""}</div>` +
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
  return `<div class="hltb"><div class="hltb-head">📖 Visual novel (VNDB)</div>${rows}` +
    (v.url ? `<a class="hltb-link" href="${escapeHtml(v.url)}" target="_blank" rel="noopener">View on VNDB ↗</a>` : "") +
    `</div>`;
}

function salesHtml(key) {
  const v = VGC[key];
  if (!v || v.units == null) return "";
  const rows = [["Shipped", v.shipped], ["Sold", v.sold]].filter(([, x]) => x != null)
    .map(([l, x]) => `<div class="hltb-row"><span>${l}</span><b>${x.toLocaleString()}</b></div>`).join("");
  return `<div class="hltb"><div class="hltb-head">📈 Sales (VGChartz)</div>${rows}` +
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
  return `<div class="hltb"><div class="hltb-head">🔬 ${escapeHtml(t.platform || "Thumby")}</div>` +
    (art ? `<div class="adb-arts">${art}</div>` : "") + vid +
    (t.description ? `<p class="thumby-desc">${escapeHtml(t.description)}</p>` : "") +
    (t.url ? `<a class="hltb-link" href="${escapeHtml(t.url)}" target="_blank" rel="noopener">View on GitHub ↗</a>` : "") +
    `</div>`;
}

// Steam extras — all keyed on the appid, so if they're here they're right.
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
  return `<div class="hltb"><div class="hltb-head">🐧 Steam</div>${deck}${proton}${rev}${own}${ccu}${ach}` +
    (x.protonUrl ? `<a class="hltb-link" href="${escapeHtml(x.protonUrl)}" target="_blank" rel="noopener">View on ProtonDB ↗</a>` : "") +
    `</div>`;
}

// The world record, next to HowLongToBeat: a nice sense of scale.
function speedrunHtml(key) {
  const r = SRC[key];
  if (!r || !r.wrTime) return "";
  const rows = (r.categories || []).slice(0, 3).map((c) =>
    `<div class="hltb-row"><span>${escapeHtml(c.category)}</span><b>${escapeHtml(c.time)}</b></div>`).join("");
  return `<div class="hltb"><div class="hltb-head">🏁 World records</div>${rows}` +
    (r.url ? `<a class="hltb-link" href="${escapeHtml(r.url)}" target="_blank" rel="noopener">Leaderboards on speedrun.com ↗</a>` : "") +
    `</div>`;
}

function guidesHtml(key) {
  const g = GDC[key];
  if (!g) return "";
  const secs = (g.sections || []).slice(0, 6)
    .map((x) => `<span class="chip">${escapeHtml(x)}</span>`).join("");
  return `<div class="hltb"><div class="hltb-head">📖 Guide (StrategyWiki)</div>` +
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
  // a fallback — it's a picture of a HUD.
  const art = (detail.artworks || [])[0] || (detail.screenshots || [])[0];
  const size = (detail.artworks || []).length ? "1080p" : "screenshot_med";
  if (bg && art) {
    bg.style.backgroundImage = `url("${IMG(art, size)}")`;
    bg.classList.add("on");
  }
  const cs = coverSrc(detail, "cover_big");
  if (coverEl && cs && coverEl.tagName !== "IMG") {
    const img = document.createElement("img");
    img.className = "cover-big"; img.id = "heroCover"; img.alt = ""; img.src = cs;
    coverEl.replaceWith(img);
  }
  if (chipsEl) {
    const rating = detail.rating != null
      ? `<span class="chip score ${ratingClass(detail.rating)}">★ ${Math.round(detail.rating * 100)} IGDB</span>` : "";
    chipsEl.innerHTML = rating
      + chips(detail.genres, "__igdb_genre") + chips(detail.themes, "__igdb_theme")
      + chips(detail.gameModes, "__igdb_mode");
  }
}

function renderIgdbSection(key, el, status, detail) {
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
    + steamxHtml(key) + arcadeHtml(key) + vndbHtml(key) + thumbyHtml(key) + guidesHtml(key)
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
    if (j.stats) updateEnrichStatus(j.stats);
    if (changed) {
      // Several health checks read the enrichment map (missing metadata, HLTB
      // mismatches), and its results are cached — so they must be recomputed
      // once enrichment lands, or "no metadata" reads as "all 14,747 games".
      resetHealth();
      // Patch in place rather than re-rendering (which would flicker every image).
      if (activeTab === "stats") renderStats();
      else if (activeTab === "home") patchHomeCovers();   // in place: a full re-render flickers
      else if (activeTab === "reviews") patchReviewCovers();
      else if (activeTab === "challenges") renderChallenges();
      else if (activeTab === "health") renderHealth();
      else if (activeTab !== "pick") { patchEnrichedCells(); renderFacets(); }
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

// Virtual facets sourced from IGDB enrichment (array-valued, joined via row._k).
const IGDB_FACET_DEFS = [
  { key: "__igdb_genre", label: "IGDB Genre", source: "genres" },
  { key: "__igdb_theme", label: "Theme", source: "themes" },
  { key: "__igdb_mode", label: "Game Mode", source: "gameModes" },
];
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

function launchHtml(row) {
  const t = launchTarget(row);
  if (!t) return "";
  const external = /^https?:/.test(t.href);
  return t.kind === "launch"
    ? `<a class="btn launch" href="${escapeHtml(t.href)}">${escapeHtml(t.label)}</a>`
    : `<a class="btn ghost" href="${escapeHtml(t.href)}"${external ? ' target="_blank" rel="noopener"' : ""}>${escapeHtml(t.label)}${external ? " ↗" : ""}</a>`;
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
function extraFacetCols() {
  if (activeTab !== "games") return [];
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
      getVals: (r) => { const e = ENRICH[r._k]; return e && e.protonTier ? [e.protonTier] : []; } },
    { key: "__steamrev", label: "Steam reviews", type: "text", facet: true, virtual: true, kind: "bucket",
      buckets: METACRITIC_BUCKETS, getVal: (r) => { const e = ENRICH[r._k]; return e && e.steamReview; } },
  ];
}
const facetCols = () => [...columns().filter((c) => c.facet), ...igdbFacetCols(), ...extraFacetCols()];
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
function matchesSearch(row, terms, cols) {
  if (!terms.length) return true;
  const hay = rowHaystack(row, cols);
  return terms.every((t) => hay.includes(t));
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
      body.appendChild(fi);
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
  const x = a[spec.key], y = b[spec.key];
  if (spec.kind === "playingRank") return playingRank(x) - playingRank(y);
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
function sortRows(rows) {
  const spec = effectiveSort();
  return [...rows].sort((a, b) => {
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
  const sorted = sortRows(rows);
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  if (st.page > pages) st.page = pages;
  const start = (st.page - 1) * PAGE_SIZE;
  const pageRows = sorted.slice(start, start + PAGE_SIZE);

  $("#tablewrap").hidden = viewMode !== "table";
  $("#gridwrap").hidden = viewMode !== "grid";
  $("#gridsortwrap").hidden = false;    // sort control in both views (reaches
  populateGridSort();                   // non-primary columns like Date Added)
  if (!sorted.length) {
    const filtered = st.search || Object.keys(st.facets).length;
    const host = viewMode === "grid" ? $("#grid") : $("#tbody");
    host.innerHTML = viewMode === "grid"
      ? emptyState("No games match", filtered ? "Try loosening a filter or clearing the search." : "Nothing here yet.", filtered ? "Clear filters" : null)
      : `<tr><td colspan="99">${emptyState("No games match", "Try loosening a filter.", null)}</td></tr>`;
    if (viewMode === "grid") $("#thead").innerHTML = "";
    const act = $("#emptyAction");
    if (act) act.onclick = () => { st.search = ""; st.facets = {}; st.page = 1; $("#search").value = ""; renderAll(); nav(); };
  } else if (viewMode === "grid") renderGrid(pageRows);
  else renderTableView(pageRows);

  maybeEnrich(pageRows);
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
  if (cv != null) parts.push("💵 $" + cv.toFixed(2));
  const units = salesOf(row);
  if (units != null) parts.push("📈 " + fmtUnits(units));
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
      : `<div class="card-cover ph${pend ? " skel" : ""}">${pend ? "" : "🎮"}</div>`;
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
let previewTimer = null, previewCard = null;

function stopPreview() {
  clearTimeout(previewTimer);
  previewTimer = null;
  if (!previewCard) return;
  const frame = previewCard.querySelector(".card-preview");
  if (frame) frame.remove();
  previewCard.classList.remove("previewing");
  previewCard = null;
}

function startPreview(card) {
  const row = CARD_ROW.get(card);
  const vid = row && (ENRICH[row._k] || {}).video;
  if (!vid || card === previewCard) return;
  stopPreview();
  previewCard = card;
  // Somewhere in the middle: trailers run ~1–2 minutes and the interesting part
  // is never at the front. We can't know the duration, so pick a safe window.
  const start = 15 + Math.floor(Math.random() * 45);
  const frame = document.createElement("iframe");
  frame.className = "card-preview";
  frame.src = `https://www.youtube-nocookie.com/embed/${vid}` +
    `?autoplay=1&mute=1&controls=0&modestbranding=1&playsinline=1&rel=0` +
    `&disablekb=1&iv_load_policy=3&fs=0&start=${start}`;
  frame.allow = "autoplay; encrypted-media";
  frame.tabIndex = -1;
  frame.setAttribute("aria-hidden", "true");
  card.appendChild(frame);
  card.classList.add("previewing");
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
    } else if (!cs && cur && ENRICH_COMPLETE && cur.classList.contains("skel")) {
      cur.classList.remove("skel");                    // resolved with no cover
      cur.textContent = "🎮";
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
  const cols = columns().filter((c) => c.sort);   // all sortable, not just shown
  const eff = effectiveSort();
  const usingDefault = !(tabState[activeTab].sort && tabState[activeTab].sort.length);
  const cur = usingDefault ? "__default" : eff[0].key;
  sel.innerHTML = `<option value="__default">Default</option>` +
    cols.map((c) => `<option value="${c.key}">${escapeHtml(c.label)}</option>`).join("");
  sel.value = cols.some((c) => c.key === cur) ? cur : "__default";
  $("#gridsortdir").textContent = eff[0].dir === "asc" ? "▲" : "▼";
  $("#gridsortdir").disabled = usingDefault;
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
    <div class="empty-art">🕹️</div>
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

function openDrawer(row, sheetKey) {
  stopPreview();
  drawerSheet = sheetKey || (SPECIAL_TABS.includes(activeTab) ? "games" : activeTab);
  const cols = (DATA.sheets[drawerSheet] || DATA.sheets.games).columns;
  const titleCol = cols[0];
  const body = $("#drawerBody");
  const titleText = escapeHtml(String(row[titleCol.key] ?? "Untitled"));
  let html = heroHtml(row, titleText);
  if (ENRICH_ENABLED && row._k) html += `<div id="igdbDetail" class="igdb-detail"></div>`;

  let raw = "";
  for (const c of cols) {
    if (c.key === titleCol.key || c.key === "platform") continue;
    const v = row[c.key];
    if (v === undefined || v === null || v === "") continue;
    const isNotes = c.type === "text" && String(v).length > 140;
    if (isNotes) {
      raw += `<div class="detail-row notes"><div class="k">${escapeHtml(c.label)}</div><div class="v">${escapeHtml(String(v))}</div></div>`;
    } else {
      raw += `<div class="detail-row"><div class="k">${escapeHtml(c.label)}</div><div class="v">${detailValue(c, v)}</div></div>`;
    }
  }
  html += collectionSectionHtml(row);
  // Sheet fields collapse behind a "Raw data" disclosure — the enriched view
  // leads. A grouped collection card has no sheet row of its own; its values are
  // aggregates over the members, so don't dress them up as raw data.
  if (raw && !row._collection) html += `<details class="raw-data"><summary>Raw data</summary>${raw}</details>`;
  body.innerHTML = html;
  wireCollections(body);
  $("#overlay").hidden = false;
  drawerRow = row;
  if (ENRICH_ENABLED && row._k) loadDetail(row._k, $("#igdbDetail"), 0, row);
}
function closeDrawer() { $("#overlay").hidden = true; }

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
const SPECIAL_TABS = ["home", "reviews", "stats", "pick", "challenges", "health"];
function setSpecialMode(mode) {   // null | "home" | "stats" | "pick" | "challenges"
  const special = SPECIAL_TABS.includes(mode);
  $("#stats").hidden = mode !== "stats";
  $("#picker").hidden = mode !== "pick";
  $("#challenges").hidden = mode !== "challenges";
  $("#home").hidden = mode !== "home";
  $("#reviews").hidden = mode !== "reviews";
  $("#health").hidden = mode !== "health";
  $(".resultbar").hidden = special;
  $("#pager").style.display = special ? "none" : "";
  document.querySelector(".facets").style.display = special ? "none" : "";
  // Filters/sort don't apply on Stats/Pick — leave only "back to top".
  $("#fabFilters").hidden = special;
  $("#fabSort").hidden = special;
  if (special) { setSheet(false); setFacets(false); $("#tablewrap").hidden = true; $("#gridwrap").hidden = true; }
}

function renderAll() {
  if (activeTab === "home") { setSpecialMode("home"); renderHome(); return; }
  if (activeTab === "reviews") { setSpecialMode("reviews"); renderReviews(); return; }
  if (activeTab === "stats") { setSpecialMode("stats"); renderStats(); return; }
  if (activeTab === "pick") { setSpecialMode("pick"); renderPicker(); return; }
  if (activeTab === "challenges") { setSpecialMode("challenges"); renderChallenges(); return; }
  if (activeTab === "health") { setSpecialMode("health"); renderHealth(); return; }
  setSpecialMode(null);
  renderFacets();
  currentFiltered = groupCollections(filterRows(null));
  renderTable(currentFiltered);
}

function switchTab(tab) {
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
  } else if (activeTab === "challenges") {
    if (chState.open) p.set("ch", chState.open);
  } else if (activeTab !== "stats") {
    const st = tabState[activeTab];
    if (viewMode !== "grid") p.set("view", viewMode);
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
  const tab = ["home", "games", "completed", "onOrder", "reviews", "stats", "pick", "challenges", "health"].includes(p.get("tab")) ? p.get("tab") : "home";
  if (SPECIAL_TABS.includes(tab)) {
    if (tab === "pick") { pickState.selector = p.get("sel") || pickState.selector; pickState.param = p.get("pp") || ""; }
    if (tab === "challenges") { chState.open = p.get("ch") || null; chState.showAll = null; }
    applyingState = false; switchTab(tab); return;
  }
  viewMode = p.get("view") === "table" ? "table" : "grid";
  PAGE_SIZE = parseInt(p.get("ps"), 10) || 50;
  const st = tabState[tab];
  st.search = p.get("q") || "";
  st.page = parseInt(p.get("page"), 10) || 1;
  const sort = p.get("sort");
  st.sort = sort ? sort.split(",").map((s) => { const [key, dir] = s.split(":"); return { key, dir: dir === "asc" ? "asc" : "desc" }; }) : null;
  st.facets = {};
  for (const [k, v] of p.entries()) if (k.startsWith("f.")) st.facets[k.slice(2)] = new Set(v.split("~"));
  $("#pagesize").value = String(PAGE_SIZE);
  $("#viewTable").classList.toggle("active", viewMode === "table");
  $("#viewGrid").classList.toggle("active", viewMode === "grid");
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
  for (const k of Object.keys(_cmdkFacets)) delete _cmdkFacets[k];
  const en = DATA.meta && DATA.meta.enrichment;
  ENRICH_ENABLED = !!(en && en.enabled !== false);
  ENRICH_SOURCES = en && en.sources ? Object.keys(en.sources) : [];
  if (ENRICH_ENABLED) updateEnrichStatus(en);
  setFreshness();
  applyStateFromURL();          // restore tab/filters/sort/view from the URL
  loadAllEnrichment();          // global covers + IGDB facets (polls during backfill)
  loadValueHistory();           // daily collection-value snapshots (for the trend chart)
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
const statCard = (v, l, pre = "", post = "") => {
  const num = typeof v === "number" && isFinite(v);
  const body = num
    ? `<div class="s-num" data-n="${v}" data-pre="${escapeHtml(pre)}" data-post="${escapeHtml(post)}">${escapeHtml(pre)}0${escapeHtml(post)}</div>`
    : `<div class="s-num">${v == null ? "—" : escapeHtml(String(v))}</div>`;
  return `<div class="stat-card">${body}<div class="s-cap">${escapeHtml(l)}</div></div>`;
};
const statPanel = (title, body, cls = "") => `<div class="stat-panel ${cls}"><h3>${escapeHtml(title)}</h3>${body}</div>`;

const usd = (v) => "$" + Math.round(v).toLocaleString();
const yr2 = (y) => `'${String(y).slice(2)}`;
const yearOf = (iso) => (typeof iso === "string" && /^\d{4}/.test(iso) ? +iso.slice(0, 4) : null);
const bucketize = (data, buckets, val) => buckets.map(([label, lo, hi]) => ({ label, value: data.filter((r) => { const v = val(r); return v != null && v >= lo && v < hi; }).length }));

// ---- Year in review + backlog burn-down ---------------------------------
const statsState = { year: null };
let VALUE_HISTORY = null;          // [{day,total,games,priced}] — daily snapshots

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
  const byDay = {};
  mine.forEach((r) => { if (/^\d{4}-\d{2}-\d{2}/.test(String(r.date))) byDay[String(r.date).slice(0, 10)] = (byDay[String(r.date).slice(0, 10)] || 0) + 1; });
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
    ${statPanel(`Every day of ${y}`, heatmap(byDay, y, { onDay: showDay }), "wide")}
    ${statPanel(`The best of ${y}`, posterRow(top, { note: (r) => `${Math.round(r.rating * 100)}%` }), "wide")}
    ${statPanel(`What you played in ${y}`, donut(topCounts(mine.map((r) => r.genre), 7)))}
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

function renderStats() {
  const rows = (DATA.sheets.completed || { rows: [] }).rows;
  const games = ((DATA.sheets.games || {}).rows) || [];
  const host = $("#stats");
  if (!rows.length && !games.length) { host.innerHTML = emptyState("No data yet", "The spreadsheet hasn’t loaded."); return; }
  resetChartLinks();

  // Counts of a field, as bars that filter that tab when clicked.
  const countBars = (src, field, n, tab) =>
    topCounts(src.map((r) => r[field]), n).map((d) => ({ ...d, link: facetLink(tab, field, d.label) }));

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
  // Running total of everything ever finished.
  const cumYears = [...new Set(rows.map((r) => yearOf(r.date)).filter(Boolean))].sort((a, b) => a - b);
  let run = 0;
  const cumulative = cumYears.map((yy) => {
    run += rows.filter((r) => yearOf(r.date) === yy).length;
    return { label: yr2(yy), value: run };
  });
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
    .map(([g, n]) => ({ label: g, value: gSum.get(g) / n, hint: `${Math.round((gSum.get(g) / n) * 100)}% over ${n} games` }))
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
  const topSales = games.map((r) => ({ r, v: salesOf(r) })).filter((x) => x.v != null)
    .sort((a, b) => b.v - a.v).slice(0, 10)
    .map((x) => ({ label: x.r.title, value: x.v, link: gameLink(x.r, "games") }));

  const sect = (title, panels) => `<h2 class="stat-sec">${title}</h2><div class="stat-grid">${panels.join("")}</div>`;
  host.innerHTML =
    yearInReview(rows, games) +
    burnDown(rows, games) +
    `<h2 class="stat-sec">All time</h2>` +
    `<div class="stat-cards">
      ${statCard(rows.length, "Completed")}
      ${statCard(Math.round(hours), "Hours played", "", "h")}
      ${statCard(avg != null ? Math.round(avg * 100) : null, "Avg rating", "", "%")}
      ${statCard(avg != null && avgCrit != null ? `${Math.round(avg * 100)}/${Math.round(avgCrit * 100)}` : "—", "You vs critics")}
      ${statCard(thisYear, "Done in " + (curYear || "—"))}
      ${statCard(backlog.length, "In backlog")}
      ${statCard(Math.round(backlogHours), "Backlog hours", "", "h")}
      ${statCard(complPct, "Library done", "", "%")}
      ${statCard(Math.round(totalSpent), "Total spent", "$")}
      ${statCard(Math.round(collectionVal), "Collection value", "$")}
      ${statCard(avgGapMo != null ? avgGapMo : null, "Avg buy→finish", "", " mo")}
    </div>` +
    sect("Completed games", [
      statPanel("Games finished, cumulatively", areaLine(cumulative, { color: 0, label: `${rows.length.toLocaleString()} all told` }), "wide"),
      statPanel("Your hall of fame", posterRow(bestRows, { note: (r) => `${Math.round(r.rating * 100)}%` }), "wide"),
      statPanel("You vs the critics", scatter(scatterPts, { xLabel: "Critics", yLabel: "You" })),
      statPanel("Your taste, by genre", radar(genreRadar, { color: 4 })),
      statPanel("Completions per year", barsV(yearData), "wide"),
      statPanel("Completions by month", barsV(monthData, { color: 1 })),
      statPanel("By release decade", barsV(decadeData, { color: 4 })),
      statPanel("Top platforms", barsH(countBars(rows, "platform", 10, "completed"))),
      statPanel("Top genres", barsH(countBars(rows, "genre", 12, "completed"))),
      statPanel("Top franchises", barsH(countBars(rows, "franchise", 10, "completed"))),
      statPanel("Top developers", barsH(countBars(rows, "developer", 10, "completed"))),
      statPanel("Top publishers", barsH(countBars(rows, "publisher", 10, "completed"))),
      statPanel("Rating distribution", barsH(ratingData)),
      statPanel("By region", donut(countBars(rows, "region", 8, "completed"))),
      statPanel("How I played", barsH(flags)),
      statPanel("Longest playthroughs", barsH(longest, { fmt: (v) => v + "h" })),
      statPanel("Biggest me-vs-critic gaps", barsH(gaps, { fmt: (v) => (v > 0 ? "+" : "") + v })),
    ]) +
    sect("Backlog", [
      statPanel("Backlog by platform", barsH(countBars(backlog, "platform", 10, "games"))),
      statPanel("Backlog by genre", barsH(countBars(backlog, "genre", 12, "games"))),
      statPanel("Backlog by length", barsH(backlogTime)),
      statPanel("Backlog by status", donut(countBars(backlog, "playingStatus", 6, "games"))),
    ]) +
    sect("Purchases & collection", [
      statPanel("Spending per year", barsV(spendData, { color: 3, fmt: usd }), "wide"),
      statPanel("Games bought per year", barsV(boughtData, { color: 5 })),
      statPanel("Cumulative spend", areaLine(cumSpend, { color: 3, label: usd(totalSpent) + " all in" }), "wide"),
      ...(VALUE_HISTORY && VALUE_HISTORY.length > 1
        ? [statPanel("Collection value over time",
            areaLine(VALUE_HISTORY.map((h) => ({ label: fmtDate(h.day).replace(/,.*/, ""), value: Math.round(h.total) })),
              { color: 2, fmt: usd, label: usd(VALUE_HISTORY[VALUE_HISTORY.length - 1].total) + " today" }), "wide")]
        : [statPanel("Collection value over time",
            `<div class="s-empty">Recording daily from today — a trend needs at least two points.
             ${VALUE_HISTORY && VALUE_HISTORY.length ? `First snapshot: ${escapeHtml(fmtDate(VALUE_HISTORY[0].day))} at ${usd(VALUE_HISTORY[0].total)}.` : ""}</div>`, "wide")]),
      statPanel("The crown jewels", posterRow(topValueRows, { note: (r) => usd(collectionValueOf(r)) }), "wide"),
      statPanel("Most valuable owned", barsH(topValue, { fmt: usd })),
      statPanel("Best selling (VGChartz)", barsH(topSales, { fmt: fmtUnits })),
      statPanel("Purchases by platform", barsH(countBars(purchases, "platform", 10, "games"))),
    ]);
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
const pickState = { selector: "backlog", param: "", picked: null };
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
const SELECTORS = [
  { id: "backlog", label: "Anything in my backlog", group: "General", filter: () => true },
  { id: "neverstarted", label: "Never started", group: "General", filter: (r) => r.owned && !r.dateStarted && !r.playingStatus },
  { id: "unfinished", label: "Started but unfinished", group: "General", filter: (r) => r.dateStarted && !r.completed },
  { id: "recentadd", label: "Recently added", group: "General", filter: (r) => !!r.dateAdded, topBy: { by: (r) => r.dateAdded, desc: true, take: 150 } },
  { id: "aging", label: "Longest in my backlog", group: "General", filter: (r) => !!(r.datePurchased || r.dateAdded), topBy: { by: (r) => r.datePurchased || r.dateAdded, desc: false, take: 150 } },

  { id: "playing", label: "Currently playing", group: "Status", filter: (r) => r.playingStatus === "Playing" },
  { id: "upnext", label: "Up next", group: "Status", filter: (r) => r.playingStatus === "Up Next" },
  { id: "onhold", label: "On hold", group: "Status", filter: (r) => r.playingStatus === "On Hold" },
  { id: "priority", label: "High priority", group: "Status", filter: (r) => Number(r.priority) >= 4 },
  { id: "maxpriority", label: "Top priority", group: "Status", filter: (r) => Number(r.priority) >= 5 },

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

  { id: "coop", label: "Co-op", group: "Play style", filter: (r) => modeIncludes(r, "co-op") || modeIncludes(r, "cooperative") },
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
function pickPool() {
  const sel = SELECTORS.find((s) => s.id === pickState.selector) || SELECTORS[0];
  let pool = pickEligible().filter((r) => (sel.param ? pickState.param && sel.filter(r, pickState.param) : sel.filter(r)));
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
  const cover = cs ? `<img src="${cs}" alt="">` : `<div class="pick-ph">🎮</div>`;
  const pt = playtimeOf(row), mc = metacriticOf(row);
  const bits = [row.platform, row.releaseYear, row.genre].filter((x) => x != null && x !== "").map((x) => escapeHtml(String(x)));
  if (pt != null) bits.push("⏱ " + fmtHours(pt));
  if (mc != null) bits.push("★ " + Math.round(mc * 100));
  return `<div class="pick-card">${cover}<div class="pick-info"><h2>${escapeHtml(String(row.title))}</h2>
    <div class="pick-meta">${bits.join(" · ")}</div>
    <div class="pick-actions"><button class="pick-reroll" id="pickReroll">🎲 Re-roll</button>
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
  if (sel.param) {
    // Rank values by how many backlog games each has (keeps big lists usable).
    const vals = topCounts(pickEligible().map((r) => r[sel.param]), 200).map((x) => `${x.label}`);
    if (!vals.includes(pickState.param)) pickState.param = vals[0] || "";
    paramHtml = `<select id="pickParam">${vals.map((v) => `<option ${v === pickState.param ? "selected" : ""}>${escapeHtml(v)}</option>`).join("")}</select>`;
  }
  const { pool } = pickPool();
  host.innerHTML = `
    <div class="pick-controls">
      <label>Pick <select id="pickSel">${opts}</select></label>${paramHtml}
      <button id="pickBtn" class="pick-btn">🎲 Pick for me</button>
      <span class="pick-count">${pool.length.toLocaleString()} game${pool.length === 1 ? "" : "s"} in pool</span>
    </div>
    <div class="pick-result" id="pickResult">${pickState.picked && pool.includes(pickState.picked)
      ? pickCard(pickState.picked)
      : `<div class="pick-empty">${pool.length ? "Hit “Pick for me” to roll a game." : "No backlog games match this selector."}</div>`}</div>`;
  $("#pickSel").onchange = (e) => { pickState.selector = e.target.value; pickState.param = ""; pickState.picked = null; renderPicker(); nav(); };
  const pp = $("#pickParam");
  if (pp) pp.onchange = (e) => { pickState.param = e.target.value; pickState.picked = null; renderPicker(); nav(); };
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
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) { switchTab(e.target.dataset.tab); nav(); } });
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
  viewMode = mode;
  $("#viewTable").classList.toggle("active", mode === "table");
  $("#viewGrid").classList.toggle("active", mode === "grid");
  renderTable(currentFiltered);
}
$("#viewTable").addEventListener("click", () => { setView("table"); nav(); });
$("#viewGrid").addEventListener("click", () => { setView("grid"); nav(); });
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
  $("#theme").textContent = t === "dark" ? "☀" : "☾";
  $("#theme").title = t === "dark" ? "Switch to light" : "Switch to dark";
}
applyTheme(currentTheme());
$("#theme").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
  if (activeTab === "stats") renderStats();     // recolour the charts' text
});

// ---- command palette (⌘K / Ctrl-K) ---------------------------------------
// 14.7k games is too many to browse to. Type a few letters, hit enter.
const cmdk = { open: false, sel: 0, results: [] };

function cmdkCandidates(q) {
  const out = [];
  const needle = q.toLowerCase().trim();
  if (!needle) {
    return [
      { kind: "Tab", label: "🏠 Home", run: () => switchTab("home") },
      { kind: "Tab", label: "🎮 All Games", run: () => switchTab("games") },
      { kind: "Tab", label: "🏆 Completed", run: () => switchTab("completed") },
      { kind: "Tab", label: "📦 On Order", run: () => switchTab("onOrder") },
      { kind: "Tab", label: "📝 Reviews", run: () => switchTab("reviews") },
      { kind: "Tab", label: "📊 Stats", run: () => switchTab("stats") },
      { kind: "Tab", label: "🎲 Pick", run: () => switchTab("pick") },
      { kind: "Tab", label: "🎯 Challenges", run: () => switchTab("challenges") },
      { kind: "Tab", label: "🩺 Health", run: () => switchTab("health") },
    ];
  }
  // Tabs
  const tabs = [["home", "🏠 Home"], ["games", "🎮 All Games"], ["completed", "🏆 Completed"],
                ["onOrder", "📦 On Order"], ["reviews", "📝 Reviews"], ["stats", "📊 Stats"],
                ["pick", "🎲 Pick"], ["challenges", "🎯 Challenges"], ["health", "🩺 Health"]];
  for (const [id, label] of tabs) {
    if (label.toLowerCase().includes(needle)) out.push({ kind: "Tab", label, run: () => switchTab(id) });
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
      ? (cover ? `<img src="${escapeHtml(cover)}" alt="">` : `<span class="cmdk-ph">🎮</span>`)
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
  for (const t of TABS) tabState[t] = { search: "", facets: {}, expanded: {}, sort: null, page: 1 };
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
  if (k === "__default") st.sort = null;
  else { const c = colByKey(k); st.sort = [{ key: k, dir: c && c.type === "text" ? "asc" : "desc", type: c && c.type }]; }
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
