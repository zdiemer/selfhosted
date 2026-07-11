"use strict";

/* IGDB relationships — what the spreadsheet can't express.

   A sheet row is one (game × platform × region). IGDB has one id per GAME, plus
   a real graph on top of it: parent/child, DLC, expansions, remakes, remasters,
   ports, bundles, editions. Two things fall out of that:

   1. A GROUPED VIEW. Rows sharing an IGDB id are the same game — Persona 5 Royal
      on PC, PS4 and Switch is three rows and one game. Grouped, they collapse to
      one card with the platforms listed on it.

   2. A RELATED-GAMES map on the detail card, with the crucial bit a raw IGDB
      dump wouldn't give you: which of the related games are IN YOUR COLLECTION,
      and whether you've finished them. "Resident Evil 4 has a remake — you own
      it, and you've beaten it. It has an expansion, Separate Ways — you don't
      have it."

   Loaded after app.js; shares its globals. */

// ---- who owns what -------------------------------------------------------
// igdbId -> the rows in your collection that matched it.
let _byIgdb = null;
const resetRelations = () => { _byIgdb = null; _relById = null; };

function rowsByIgdbId() {
  if (_byIgdb) return _byIgdb;
  const m = new Map();
  for (const r of ((DATA.sheets.games || {}).rows || [])) {
    const id = (ENRICH[r._k] || {}).igdbId;
    if (!id) continue;
    if (!m.has(id)) m.set(id, []);
    m.get(id).push(r);
  }
  return (_byIgdb = m);
}

// ---- grouped view --------------------------------------------------------
// Rows sharing an IGDB id are the same game. So are PORTS and their parent: IGDB
// gives a port its own id, but "Chrono Trigger (DS)" is not a different game
// from "Chrono Trigger" — it's the same game on another machine, which is
// exactly what this view exists to collapse.
//
// Remakes and remasters are deliberately NOT folded in. Resident Evil 4 (2005)
// and Resident Evil 4 (2023) really are different games — you can beat one and
// not the other, and you rate them separately. They stay apart, and the
// relationship map on the detail card is where you see they're connected.
const MERGE_TYPES = new Set(["Port"]);

// Walk a port up to the game it's a port of. Bounded: a cycle in IGDB's data
// (or a port of a port of a port) must not spin forever.
function canonicalGameId(row) {
  let id = (ENRICH[row._k] || {}).igdbId;
  if (!id) return null;
  const seen = new Set();
  for (let hop = 0; hop < 4; hop++) {
    if (seen.has(id)) break;
    seen.add(id);
    const rel = relById(id);
    if (!rel || !MERGE_TYPES.has(rel.type) || !rel.parentId) break;
    id = rel.parentId;
  }
  return id;
}

// The relation summary for an IGDB id, from whichever of our rows matched it.
let _relById = null;
function relById(id) {
  if (!_relById) {
    _relById = new Map();
    for (const e of Object.values(ENRICH)) {
      if (e && e.igdbId && e.rel && !_relById.has(e.igdbId)) _relById.set(e.igdbId, e.rel);
    }
  }
  return _relById.get(id);
}

function groupByGame(rows) {
  const out = [], seen = new Map();
  for (const r of rows) {
    const id = canonicalGameId(r);
    if (!id) { out.push(r); continue; }          // no IGDB match: can't claim it's the same game
    if (seen.has(id)) { seen.get(id).push(r); continue; }
    const members = [r];
    seen.set(id, members);
    out.push({ _group: id, _members: members });
  }
  // A "group" of one is just the row itself; don't wrap it in ceremony.
  return out.map((x) => (x._group && x._members.length === 1 ? x._members[0] : x))
    .map((x) => (x._group ? groupRow(x) : x));
}

// The synthetic row standing in for a group of editions.
function groupRow(g) {
  const ms = g._members;
  // The card should be titled after the GAME, so prefer a row that matched the
  // canonical id itself (the original) over one of its ports. Then prefer the
  // copy you've actually played, then one you own, then the newest.
  const originals = ms.filter((r) => (ENRICH[r._k] || {}).igdbId === g._group);
  const pool = originals.length ? originals : ms;
  const lead = pool.find((r) => r.completed) || pool.find((r) => r.owned) ||
    pool.slice().sort((a, b) => String(b.releaseDate || "").localeCompare(String(a.releaseDate || "")))[0];
  const platforms = [...new Set(ms.map((r) => r.platform).filter(Boolean))];
  return {
    ...lead,
    _k: lead._k,
    _groupId: g._group,
    _members: ms,
    _platforms: platforms,
    completed: ms.some((r) => r.completed),
    owned: ms.some((r) => r.owned),
    platform: platforms.length > 1 ? `${platforms.length} platforms` : lead.platform,
  };
}

// ---- the detail card's relationship map ----------------------------------
const REL_SECTIONS = [
  ["parent", "Part of", true],
  ["versionParent", "Edition of", true],
  ["expandedGames", "Expanded editions", false],
  ["remakes", "Remakes", false],
  ["remasters", "Remasters", false],
  ["ports", "Ports", false],
  ["expansions", "Expansions", false],
  ["standaloneExpansions", "Standalone expansions", false],
  ["dlcs", "DLC", false],
  ["bundles", "Bundles", false],
];

// A related game, annotated with whether it's in your collection.
function relCardHtml(entry) {
  const mine = rowsByIgdbId().get(entry.id) || [];
  const row = mine.find((r) => r.completed) || mine.find((r) => r.owned) || mine[0];
  const cover = entry.cover ? IMG(entry.cover, "cover_small") : (row ? coverSrc(ENRICH[row._k], "cover_small") : "");
  const state = !row ? "none" : row.completed ? "done" : row.owned ? "owned" : "listed";
  const badge = { done: "✓ Beaten", owned: "● Owned", listed: "In your list", none: "Not in your collection" }[state];
  const art = cover
    ? `<img loading="lazy" src="${escapeHtml(cover)}" alt="">`
    : `<span class="rl-ph">🎮</span>`;
  return `<button class="rl-card rl-${state}"${row ? ` data-rlk="${escapeHtml(String(row._k))}"` : ""}
      title="${escapeHtml(entry.name)}">
    ${art}
    <span class="rl-txt">
      <b>${escapeHtml(entry.name)}</b>
      <span class="rl-badge rl-b-${state}">${badge}</span>
    </span>
  </button>`;
}

function relationsHtml(detail) {
  const rel = detail && detail.relations;
  if (!rel) return "";

  const sections = [];
  for (const [key, label, single] of REL_SECTIONS) {
    const v = rel[key];
    const list = single ? (v ? [v] : []) : (v || []);
    if (!list.length) continue;
    sections.push(`<div class="rl-sect">
      <h4>${escapeHtml(label)}<span class="muted">${single ? "" : ` ${list.length}`}</span></h4>
      <div class="rl-row">${list.map(relCardHtml).join("")}</div>
    </div>`);
  }
  if (!sections.length) return "";

  const kind = rel.gameTypeLabel && rel.gameTypeLabel !== "Main game"
    ? `<span class="rl-kind">${escapeHtml(rel.gameTypeLabel)}${rel.versionTitle ? ` · ${escapeHtml(rel.versionTitle)}` : ""}</span>` : "";
  return `<div class="rl">
    <div class="rl-head"><h3>🔗 Related games</h3>${kind}</div>
    ${sections.join("")}
  </div>`;
}

// Clicking a related game you own opens it.
function wireRelations(scope) {
  scope.querySelectorAll("[data-rlk]").forEach((el) => {
    el.onclick = () => {
      const row = ((DATA.sheets.games || {}).rows || []).find((r) => String(r._k) === el.dataset.rlk);
      if (row) openDrawerFrom(row, "games");     // navigation: keep a way back
    };
  });
}

// The other editions of THIS game that you own — shown on the grouped card and
// in the drawer, so a group is never a black box.
function editionsHtml(row) {
  const ms = row._members;
  if (!ms || ms.length < 2) return "";
  return `<div class="rl">
    <div class="rl-head"><h3>🗂 Your copies <span class="muted">${ms.length}</span></h3></div>
    <div class="rl-copies">${ms.map((m, i) => {
      const e = ENRICH[m._k] || {};
      const kind = e.rel && e.rel.type && e.rel.type !== "Main game" ? e.rel.type : "";
      const bits = [m.platform, m.releaseRegion, m.releaseYear].filter(Boolean)
        .map((x) => escapeHtml(String(x))).join(" · ");
      const mark = m.completed ? `<span class="rl-b-done">✓ Beaten</span>`
        : m.owned ? `<span class="rl-b-owned">● Owned</span>` : "";
      return `<button class="rl-copy" data-rlc="${i}">
        <span class="rl-copy-t">${bits}${kind ? ` <span class="rl-tag">${escapeHtml(kind)}</span>` : ""}</span>${mark}</button>`;
    }).join("")}</div>
  </div>`;
}
