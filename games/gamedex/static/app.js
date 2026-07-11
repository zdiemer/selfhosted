"use strict";

// ---- config -------------------------------------------------------------
const PAGE_SIZE = 50;
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
    default:
      return escapeHtml(String(value));
  }
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---- data access --------------------------------------------------------
const sheet = () => DATA.sheets[activeTab];
const columns = () => sheet().columns;
const searchCols = () => columns().filter((c) => c.search).map((c) => c.key);
const facetCols = () => columns().filter((c) => c.facet);
const colByKey = (key) => columns().find((c) => c.key === key);

// Facet value display label + sortable key for a raw value.
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
// Row matches a facet selection (Set of String(value)). OR within a facet.
function matchesFacet(row, key, selected) {
  if (!selected || selected.size === 0) return true;
  const v = row[key];
  if (v === undefined || v === null) return false;
  return selected.has(String(v));
}

// Rows matching search + every facet EXCEPT `skipKey` (for facet counts) or all.
function filterRows(skipKey) {
  const st = tabState[activeTab];
  const terms = st.search.toLowerCase().split(/\s+/).filter(Boolean);
  const sCols = searchCols();
  const facetKeys = Object.keys(st.facets);
  return sheet().rows.filter((row) => {
    if (!matchesSearch(row, terms, sCols)) return false;
    for (const key of facetKeys) {
      if (key === skipKey) continue;
      if (!matchesFacet(row, key, st.facets[key])) return false;
    }
    return true;
  });
}

// ---- rendering: facets --------------------------------------------------
function renderFacets() {
  const st = tabState[activeTab];
  const host = $("#facets");
  host.innerHTML = "";

  for (const col of facetCols()) {
    // Count values across rows filtered by the OTHER facets + search.
    const base = filterRows(col.key);
    const counts = new Map();
    for (const row of base) {
      const v = row[col.key];
      if (v === undefined || v === null || v === "") continue;
      const k = String(v);
      counts.set(k, (counts.get(k) || 0) + 1);
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
    const numeric = col.type === "year" || col.type === "int" || col.type === "number";
    values.sort((a, b) =>
      numeric ? Number(b.key) - Number(a.key) : b.count - a.count || a.label.localeCompare(b.label)
    );

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
const NUMERIC_TYPES = ["rating", "hours", "number", "int", "year"];

// Per-tab default sort. A spec is {key, dir, type?, kind?}; `kind` selects a
// custom comparator. The games default: Playing-status group on top
// (Playing→On Hold→Up Next→none), then uncompleted before completed, then
// newest release year, with newest release date (Early Access = newest) as the
// final tiebreaker.
const DEFAULT_SORT = {
  games: [
    { key: "playingStatus", kind: "playingRank", dir: "desc" },
    { key: "completed", dir: "asc", type: "bool" },
    { key: "releaseYear", dir: "desc", type: "year" },
    { key: "releaseDate", kind: "releaseDateDesc", dir: "desc" },
  ],
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
  if (NUMERIC_TYPES.includes(type)) return (Number(x) - Number(y)) * dir;
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
}

function renderTable(rows) {
  const st = tabState[activeTab];
  const cols = columns().filter((c) => c.primary);
  const sorted = sortRows(rows);

  // header — show sort direction + precedence number for each active level
  const thead = $("#thead");
  thead.innerHTML = "";
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

  // page slice
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  if (st.page > pages) st.page = pages;
  const start = (st.page - 1) * PAGE_SIZE;
  const pageRows = sorted.slice(start, start + PAGE_SIZE);

  const tbody = $("#tbody");
  tbody.innerHTML = "";
  for (const row of pageRows) {
    const tr = document.createElement("tr");
    tr.innerHTML = cols.map((c) => `<td>${fmtCell(row[c.key], c.type)}</td>`).join("");
    tr.onclick = () => openDrawer(row);
    tbody.appendChild(tr);
  }

  // count + pager
  $("#count").textContent = `${sorted.length.toLocaleString()} of ${sheet().rows.length.toLocaleString()} games`;
  $("#clear").hidden = !(st.search || Object.keys(st.facets).length);
  $("#resetsort").hidden = !(st.sort && st.sort.length);
  renderPager(pages);
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
    b.onclick = () => { st.page = page; renderTable(currentFiltered); window.scrollTo(0, 0); $(".tablewrap").scrollTop = 0; };
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
  setFreshness();
  switchTab("games");
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// events
$("#search").addEventListener("input", (e) => {
  const st = tabState[activeTab];
  st.search = e.target.value;
  st.page = 1;
  renderAll();
});
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) switchTab(e.target.dataset.tab); });
$("#clear").addEventListener("click", () => {
  const st = tabState[activeTab];
  st.search = ""; st.facets = {}; st.page = 1;
  $("#search").value = "";
  renderAll();
});
$("#resetsort").addEventListener("click", () => {
  tabState[activeTab].sort = null;
  tabState[activeTab].page = 1;
  renderAll();
});
$("#drawerClose").addEventListener("click", closeDrawer);
$("#overlay").addEventListener("click", (e) => { if (e.target.id === "overlay") closeDrawer(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

load().catch((err) => { console.error(err); $("#count").textContent = "Error: " + err.message; });
