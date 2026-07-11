"use strict";

// ---- config -------------------------------------------------------------
let PAGE_SIZE = 50;
let viewMode = "grid";             // "table" | "grid"
const FACET_CAP = 12;              // values shown before "show more"
const FACET_FILTER_THRESHOLD = 12; // show a per-facet search box past this many values

// ---- state --------------------------------------------------------------
let DATA = null;            // {meta, sheets}
let activeTab = "games";
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
const coverSrc = (e, size) => (e && e.coverUrl ? e.coverUrl : (e && e.cover ? IMG(e.cover, size) : ""));
let ENRICH_ENABLED = false;
let ENRICH_SOURCES = [];           // enabled secondary sources (hltb, metacritic, gameye)
const ENRICH = {};                 // matchKey -> light enrichment
const DETAIL = {};                 // matchKey -> full IGDB detail (drawer cache)
const HLTBC = {};                  // matchKey -> HLTB playtimes (drawer cache)
const MCC = {};                    // matchKey -> Metacritic score (drawer cache)
const GEC = {};                    // matchKey -> GameEye prices (drawer cache)
const ENRICH_REQUESTED = new Set();
let enrichTimer = null;
let drawerRow = null;              // row currently shown in the drawer (for sheet fallback)

function coverCell(row) {
  const src = coverSrc(ENRICH[row._k], "cover_small");
  return src ? `<img class="cover-thumb" loading="lazy" src="${src}" alt="">` : `<span class="cover-ph">🎮</span>`;
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
    if (changed) renderTable(currentFiltered);              // fill covers in place
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
}

const chips = (arr) => (arr && arr.length
  ? `<div class="chips">${arr.map((x) => `<span class="chip">${escapeHtml(String(x))}</span>`).join("")}</div>` : "");

function detailHtml(d) {
  if (!d) return "";
  const cs = coverSrc(d, "cover_big");
  const cover = cs ? `<img class="cover-big" src="${cs}" alt="">` : "";
  const badge = d.manual ? `<span class="chip manual">★ Manually mapped</span>` : "";
  const rating = d.rating != null
    ? `<div class="igdb-rating ${ratingClass(d.rating)}">${Math.round(d.rating * 100)}<small>/100 IGDB</small>${d.ratingCount ? ` · ${d.ratingCount} ratings` : ""}</div>` : "";
  const meta = [];
  if (d.developers && d.developers.length) meta.push(`<div class="detail-row"><div class="k">Developer</div><div class="v">${escapeHtml(d.developers.join(", "))}</div></div>`);
  if (d.publishers && d.publishers.length) meta.push(`<div class="detail-row"><div class="k">Publisher</div><div class="v">${escapeHtml(d.publishers.join(", "))}</div></div>`);
  const shots = (d.screenshots || []).length
    ? `<div class="shots">${d.screenshots.map((s) =>
        `<a href="${IMG(s, "screenshot_huge")}" target="_blank" rel="noopener"><img loading="lazy" src="${IMG(s, "screenshot_med")}" alt=""></a>`).join("")}</div>` : "";
  const similar = (d.similar || []).filter((s) => s.cover).slice(0, 8);
  const simHtml = similar.length
    ? `<div class="detail-row notes"><div class="k">Similar games</div><div class="similar">${similar.map((s) =>
        `<a href="${escapeHtml(s.url || "#")}" target="_blank" rel="noopener" title="${escapeHtml(s.name)}"><img loading="lazy" src="${IMG(s.cover, "cover_small")}" alt=""><span>${escapeHtml(s.name)}</span></a>`).join("")}</div></div>` : "";
  const text = d.summary || d.storyline;
  return `<div class="igdb-head">${cover}<div class="igdb-side">${badge ? `<div class="badges">${badge}</div>` : ""}${rating}
       ${chips([...(d.genres || []), ...(d.themes || [])])}
       ${chips(d.gameModes || [])}</div></div>` +
    (text ? `<div class="detail-row notes"><div class="k">Summary (IGDB)</div><div class="v">${escapeHtml(text)}</div></div>` : "") +
    meta.join("") + shots + simHtml +
    igdbAttr(d);
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
  const rows = [{ id: "igdb", label: "Metadata (IGDB)", ph: "IGDB game URL" }];
  if (ENRICH_SOURCES.includes("hltb")) rows.push({ id: "hltb", label: "HowLongToBeat", ph: "HLTB game URL" });
  if (ENRICH_SOURCES.includes("metacritic")) rows.push({ id: "metacritic", label: "Metacritic", ph: "Metacritic game URL" });
  const ownedPhys = drawerRow && drawerRow.owned && (drawerRow.format || "").toLowerCase() === "physical";
  if (ENRICH_SOURCES.includes("gameye") && ownedPhys) rows.push({ id: "gameye", label: "GameEye value", ph: "GameEye encyclopedia URL" });
  return `<details class="map-menu"><summary>🔧 Fix mapping</summary>` +
    rows.map((s) => `<div class="map-src" data-src="${s.id}"><label>${escapeHtml(s.label)}</label>
      <div class="map-row"><input type="url" placeholder="${s.ph}" data-map-input>
      <button class="btn" data-map-go>Map</button><button class="linkbtn" data-map-reset title="Reset to auto">Auto</button></div></div>`).join("") +
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

// Compose the drawer's enrichment section: IGDB + HLTB + Metacritic + GameEye + map.
function renderIgdbSection(key, el, status, detail) {
  let content;
  if (status === "matched" && detail) content = detailHtml(detail);
  else if (status === "no_match") content = `<div class="igdb-loading muted">No IGDB match for this title.</div>`;
  else {
    // loading / pending / error — show the cover from the light data immediately
    // so we never display a bare "Loading" when we already have metadata.
    const cs = coverSrc(ENRICH[key], "cover_big");
    const msg = status === "pending-final" ? "Metadata still resolving — reopen shortly."
      : status === "error" ? "Couldn’t load extra details."
      : "Loading details…";
    content = (cs ? `<div class="igdb-head"><img class="cover-big" src="${cs}" alt=""></div>` : "") +
      `<div class="igdb-loading muted">${msg}</div>`;
  }
  el.innerHTML = content + hltbHtml(HLTBC[key]) + metacriticHtml(key) + gameyeHtml(key) + mapControlHtml(key);

  el.querySelectorAll(".map-src").forEach((rowEl) => {
    const src = rowEl.dataset.src;
    const go = rowEl.querySelector("[data-map-go]");
    const input = rowEl.querySelector("[data-map-input]");
    const reset = rowEl.querySelector("[data-map-reset]");
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
  });
}

async function submitOverride(key, url, source = "igdb") {
  try {
    const res = await fetch("api/enrichment/override", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, url, source }),
    });
    if (!res.ok) return false;
    const j = await res.json();
    // Clear caches so the refetch shows the new mapping.
    delete DETAIL[key]; delete HLTBC[key]; delete MCC[key]; delete GEC[key];
    if (source === "igdb") {
      const r = j.record;
      if (r) ENRICH[key] = Object.assign(ENRICH[key] || {}, {
        cover: r.cover, coverUrl: r.coverUrl, source: r.source, genres: r.genres,
        themes: r.themes, gameModes: r.gameModes, userRating: r.userRating,
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
    if (changed) renderAll();                       // covers + facets fill in
    if (j.stats && !j.stats.complete) {             // IGDB or HLTB backfill still running
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
const METACRITIC_BUCKETS = [
  { label: "90–100", test: (v) => v >= 0.9 },
  { label: "80–89", test: (v) => v >= 0.8 && v < 0.9 },
  { label: "70–79", test: (v) => v >= 0.7 && v < 0.8 },
  { label: "60–69", test: (v) => v >= 0.6 && v < 0.7 },
  { label: "< 60", test: (v) => v < 0.6 },
];
// Best playtime for a row: HLTB (main→best) where enriched, else sheet estimate.
const playtimeOf = (row) => { const e = ENRICH[row._k]; const h = e && e.hltbBest; return h != null ? h : row.estimatedTime; };
// Metacritic (0–1): scraped score where enriched, else the sheet's Metacritic Rating.
const metacriticOf = (row) => { const e = ENRICH[row._k]; return e && e.metascore != null ? e.metascore / 100 : row.metacriticRating; };
// User rating (0–1): IGDB community rating where enriched, else sheet GameFAQs.
const userRatingOf = (row) => { const e = ENRICH[row._k]; return e && e.userRating != null ? e.userRating : row.gamefaqsUserRating; };

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

const igdbFacetCols = () =>
  ENRICH_ENABLED
    ? IGDB_FACET_DEFS.map((d) => ({ ...d, type: "text", facet: true, virtual: true }))
    : [];
// Bucketed facets available on the Games tab (playtime + Metacritic).
function extraFacetCols() {
  if (activeTab !== "games") return [];
  return [
    { key: "__playtime", label: "Playtime", type: "text", facet: true, virtual: true, kind: "bucket", buckets: PLAYTIME_BUCKETS, getVal: playtimeOf },
    { key: "__metacritic", label: "Metacritic", type: "text", facet: true, virtual: true, kind: "bucket", buckets: METACRITIC_BUCKETS, getVal: metacriticOf },
    { key: "__userrating", label: "User Rating", type: "text", facet: true, virtual: true, kind: "bucket", buckets: METACRITIC_BUCKETS, getVal: userRatingOf },
  ];
}
const facetCols = () => [...columns().filter((c) => c.facet), ...igdbFacetCols(), ...extraFacetCols()];
const facetColByKey = (key) => facetCols().find((c) => c.key === key);

// A row's facet values as [{key, raw}] — scalar → one, arrays → many, bucket → one label.
function rowFacetItems(row, col) {
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
function matchesSearch(row, terms, cols) {
  if (!terms.length) return true;
  const hay = cols.map((k) => row[k]).filter((v) => v != null).join(" ").toLowerCase();
  return terms.every((t) => hay.includes(t));
}
// Row matches a facet selection (Set of value keys). OR within a facet; for
// IGDB array facets a row matches if ANY of its values is selected.
function matchesFacet(row, col, selected) {
  if (!selected || selected.size === 0) return true;
  return rowFacetItems(row, col).some((it) => selected.has(it.key));
}

// Rows matching search + every facet EXCEPT `skipKey` (for facet counts) or all.
function filterRows(skipKey) {
  const st = tabState[activeTab];
  const terms = st.search.toLowerCase().split(/\s+/).filter(Boolean);
  const sCols = searchCols();
  const active = Object.keys(st.facets)
    .map((k) => [facetColByKey(k), st.facets[k]])
    .filter(([c]) => c);
  return sheet().rows.filter((row) => {
    if (!matchesSearch(row, terms, sCols)) return false;
    for (const [col, sel] of active) {
      if (col.key === skipKey) continue;
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
    if (values.length > FACET_FILTER_THRESHOLD) {
      const fi = document.createElement("input");
      fi.className = "facet-filter";
      fi.type = "search";
      fi.placeholder = `Filter ${col.label.toLowerCase()}…`;
      fi.value = filterText;
      fi.oninput = () => {
        st.expanded[filterKey] = fi.value;
        renderFacets();
        // keep focus after re-render
        const again = group.querySelector(".facet-filter");
        if (again) { again.focus(); again.setSelectionRange(fi.value.length, fi.value.length); }
      };
      body.appendChild(fi);
    }

    let shown = values;
    if (filterText) {
      const q = filterText.toLowerCase();
      shown = shown.filter((v) => v.label.toLowerCase().includes(q));
    }
    const capped = !showAll && !filterText && shown.length > FACET_CAP;
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
      body.appendChild(opt);
    }

    if (capped) {
      const more = document.createElement("button");
      more.className = "facet-more";
      more.textContent = `Show ${shown.length - FACET_CAP} more…`;
      more.onclick = () => { st.expanded[filterKey + "_all"] = true; renderFacets(); };
      body.appendChild(more);
    } else if (showAll && shown.length > FACET_CAP && !filterText) {
      const less = document.createElement("button");
      less.className = "facet-more";
      less.textContent = "Show less";
      less.onclick = () => { st.expanded[filterKey + "_all"] = false; renderFacets(); };
      body.appendChild(less);
    }

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
  $("#gridsortwrap").hidden = viewMode !== "grid";
  if (viewMode === "grid") { populateGridSort(); renderGrid(pageRows); }
  else renderTableView(pageRows);

  maybeEnrich(pageRows);
  $("#count").textContent = `${sorted.length.toLocaleString()} of ${sheet().rows.length.toLocaleString()} games`;
  $("#clear").hidden = !(st.search || Object.keys(st.facets).length);
  $("#resetsort").hidden = !(st.sort && st.sort.length);
  renderPager(pages);
}

function renderTableView(pageRows) {
  const cols = columns().filter((c) => c.primary);
  const thead = $("#thead");
  thead.innerHTML = "";
  if (ENRICH_ENABLED) thead.appendChild(document.createElement("th")).className = "cover-h";
  const spec = effectiveSort();
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

function renderGrid(pageRows) {
  const grid = $("#grid");
  const titleKey = (columns().find((c) => c.primary) || columns()[0]).key;
  grid.innerHTML = "";
  for (const row of pageRows) {
    const e = ENRICH[row._k];
    const cs = coverSrc(e, "cover_big");
    const cover = cs
      ? `<img class="card-cover" loading="lazy" src="${cs}" alt="">`
      : `<div class="card-cover ph">🎮</div>`;
    const title = escapeHtml(String(row[titleKey] ?? "Untitled"));
    const rel = row.releaseDate || row.release;                 // full date, else year
    const relDisp = rel ? fmtDate(rel) : row.releaseYear;
    const pt = playtimeOf(row);
    const parts = [row.platform, relDisp].filter((x) => x != null && x !== "").map((x) => escapeHtml(String(x)));
    if (pt != null) parts.push("⏱ " + fmtHours(pt));
    const cv = collectionValueOf(row);
    if (cv != null) parts.push("💵 $" + cv.toFixed(2));
    const sub = parts.join(" · ");
    const rating = row.rating != null
      ? `<span class="card-rating ${ratingClass(row.rating)}" title="My rating">${Math.round(row.rating * 100)}</span>` : "";
    const mc = metacriticOf(row);
    const meta = mc != null
      ? `<span class="card-meta ${ratingClass(mc)}" title="Metacritic">${Math.round(mc * 100)}</span>` : "";
    const card = document.createElement("div");
    card.className = "card" + (rowCompleted(row) ? " done" : "");
    card.innerHTML = `${cover}<div class="card-body">${meta}${rating}<div class="card-title" title="${title}">${title}</div><div class="card-sub">${sub}</div></div>`;
    card.onclick = () => openDrawer(row);
    grid.appendChild(card);
  }
}

// Grid has no clickable headers — a Sort dropdown + direction toggle stand in.
function populateGridSort() {
  const sel = $("#gridsort");
  const cols = columns().filter((c) => c.primary && c.sort);
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
  const mk = (label, page, disabled) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.disabled = disabled;
    b.onclick = () => {
      st.page = page; renderTable(currentFiltered);
      $("#tablewrap").scrollTop = 0; $("#gridwrap").scrollTop = 0;
      nav();
    };
    return b;
  };
  el.appendChild(mk("‹ Prev", st.page - 1, st.page <= 1));
  const info = document.createElement("span");
  info.className = "page-info";
  info.textContent = `Page ${st.page} of ${pages}`;
  el.appendChild(info);
  el.appendChild(mk("Next ›", st.page + 1, st.page >= pages));
}

// ---- detail drawer ------------------------------------------------------
function openDrawer(row) {
  const cols = columns();
  const titleCol = cols[0];
  const body = $("#drawerBody");
  const platform = row["platform"] ? `<span class="pill">${escapeHtml(String(row["platform"]))}</span>` : "";
  let html = `<h2>${escapeHtml(String(row[titleCol.key] ?? "Untitled"))}</h2><div class="subtitle">${platform}</div>`;
  if (ENRICH_ENABLED && row._k) html += `<div id="igdbDetail" class="igdb-detail"></div>`;

  for (const c of cols) {
    if (c.key === titleCol.key) continue;
    const v = row[c.key];
    if (v === undefined || v === null || v === "") continue;
    const isNotes = c.type === "text" && String(v).length > 140;
    if (isNotes) {
      html += `<div class="detail-row notes"><div class="k">${escapeHtml(c.label)}</div><div class="v">${escapeHtml(String(v))}</div></div>`;
    } else {
      html += `<div class="detail-row"><div class="k">${escapeHtml(c.label)}</div><div class="v">${fmtCell(v, c.type)}</div></div>`;
    }
  }
  body.innerHTML = html;
  $("#overlay").hidden = false;
  drawerRow = row;
  if (ENRICH_ENABLED && row._k) loadDetail(row._k, $("#igdbDetail"), 0, row);
}
function closeDrawer() { $("#overlay").hidden = true; }

// ---- orchestration ------------------------------------------------------
let currentFiltered = [];
function renderAll() {
  renderFacets();
  currentFiltered = filterRows(null);
  renderTable(currentFiltered);
}

function switchTab(tab) {
  activeTab = tab;
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === tab);
  $("#search").value = tabState[tab].search;
  renderAll();
}

// ---- URL state: back/forward + shareable/refreshable links ---------------
let applyingState = false;
function syncURL(push) {
  if (applyingState) return;
  const st = tabState[activeTab];
  const p = new URLSearchParams();
  if (activeTab !== "games") p.set("tab", activeTab);
  if (viewMode !== "grid") p.set("view", viewMode);
  if (PAGE_SIZE !== 50) p.set("ps", String(PAGE_SIZE));
  if (st.search) p.set("q", st.search);
  if (st.page > 1) p.set("page", String(st.page));
  if (st.sort && st.sort.length) p.set("sort", st.sort.map((s) => `${s.key}:${s.dir}`).join(","));
  for (const [k, set] of Object.entries(st.facets)) if (set && set.size) p.set("f." + k, [...set].join("~"));
  const qs = p.toString();
  history[push ? "pushState" : "replaceState"]({}, "", qs ? "?" + qs : location.pathname);
}
function applyStateFromURL() {
  applyingState = true;
  const p = new URLSearchParams(location.search);
  viewMode = p.get("view") === "table" ? "table" : "grid";
  PAGE_SIZE = parseInt(p.get("ps"), 10) || 50;
  const tab = ["games", "completed", "onOrder"].includes(p.get("tab")) ? p.get("tab") : "games";
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
  let payload = null;
  for (let attempt = 0; attempt < 40; attempt++) {
    const res = await fetch("api/data", { cache: "no-store" });
    if (res.ok) { payload = await res.json(); break; }
    if (res.status === 503) { $("#count").textContent = "Fetching spreadsheet from Dropbox…"; await sleep(1500); continue; }
    throw new Error(`api/data returned ${res.status}`);
  }
  if (!payload) { $("#count").textContent = "Could not load data — is the Dropbox link set?"; return; }
  DATA = payload;
  const en = DATA.meta && DATA.meta.enrichment;
  ENRICH_ENABLED = !!(en && en.enabled !== false);
  ENRICH_SOURCES = en && en.sources ? Object.keys(en.sources) : [];
  if (ENRICH_ENABLED) updateEnrichStatus(en);
  setFreshness();
  applyStateFromURL();          // restore tab/filters/sort/view from the URL
  loadAllEnrichment();          // global covers + IGDB facets (polls during backfill)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// events
$("#search").addEventListener("input", (e) => {
  const st = tabState[activeTab];
  st.search = e.target.value;
  st.page = 1;
  renderAll();
  syncURL(false);          // replace so typing doesn't flood history
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

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
