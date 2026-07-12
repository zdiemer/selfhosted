"use strict";

/* Groupings — the collection cut along whichever axis you care about.

   Series started as a page per franchise: every game in release order, what you
   own, what you've beaten, how far through you are, and the obvious next one to
   play. Nothing about that view is really ABOUT franchises, though — it works for
   any set of games that belong together. So it's now one machine with a choice of
   axis, and "Series" is simply the franchise axis.

   Three levels: pick an axis → an index of groups → the group itself.

   Loaded after app.js; shares its globals (DATA, ENRICH, openDrawer, …). */

const groupState = { kind: null, open: null, q: "", sort: "size" };

const grRows = () => ((DATA.sheets.games || {}).rows || []).filter((r) => r.title);

/* Developer / publisher / genre LOOK multi-valued but aren't: of 14,752 rows only
   93 developers contain a comma and they are names ("I, Robot", "Ether, Hidden
   Vale"), not lists. Splitting on punctuation would invent studios. One row
   belongs to exactly one group per axis. */
const GROUPINGS = [
  { id: "series", label: "Series", icon: "🎬", key: (r) => r.franchise,
    units: "franchises", blurb: "Franchises in release order. How far through Castlevania are you?" },
  { id: "developer", label: "Developers", icon: "🛠️", key: (r) => r.developer,
    units: "studios", blurb: "Every studio you own something by — and what you never got round to." },
  { id: "publisher", label: "Publishers", icon: "🏢", key: (r) => r.publisher,
    units: "publishers", blurb: "Who put it on the shelf, rather than who made it." },
  { id: "genre", label: "Genres", icon: "🎯", key: (r) => r.genre,
    units: "genres", blurb: "What you actually play, as opposed to what you buy." },
  { id: "platform", label: "Platforms", icon: "🕹️", key: (r) => r.platform,
    units: "platforms", blurb: "A shelf per machine, with how much of it you've finished." },
];
const grouping = (id) => GROUPINGS.find((g) => g.id === id);

const MIN_GROUP = 2;      // one game is not a grouping

let _grIndex = {};
const resetGroups = () => { _grIndex = {}; };

function groupIndex(kind) {
  if (_grIndex[kind]) return _grIndex[kind];
  const g = grouping(kind);
  if (!g) return [];
  const m = new Map();
  for (const r of grRows()) {
    const k = g.key(r);
    if (!k) continue;
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(r);
  }
  const out = [];
  for (const [name, games] of m) {
    // Release order is how any of these is meant to be read.
    games.sort((a, b) => String(a.releaseDate || a.releaseYear || "").localeCompare(
      String(b.releaseDate || b.releaseYear || "")));
    const done = games.filter((x) => x.completed).length;
    const owned = games.filter((x) => x.owned).length;
    // What to play next: the earliest unfinished game you can actually play.
    const next = games.find((x) => !x.completed && (typeof isCandidate === "function" ? isCandidate(x) : x.owned))
      || games.find((x) => !x.completed);
    out.push({ name, games, done, owned, next, pct: games.length ? done / games.length : 0 });
  }
  return (_grIndex[kind] = out);
}

const grGroups = (kind) => groupIndex(kind).filter((s) => s.games.length >= MIN_GROUP);

const GROUP_SORTS = {
  size: { label: "Most games", cmp: (a, b) => b.games.length - a.games.length },
  progress: { label: "Closest to done", cmp: (a, b) => (b.pct - a.pct) || (b.done - a.done) },
  done: { label: "Most finished", cmp: (a, b) => b.done - a.done },
  name: { label: "A–Z", cmp: (a, b) => a.name.localeCompare(b.name) },
  untouched: { label: "Never started", cmp: (a, b) => (a.pct - b.pct) || (b.games.length - a.games.length) },
};

// ---- cards ---------------------------------------------------------------

function groupCardHtml(s) {
  // A group has no art of its own, so it borrows from whichever member has some.
  const art = s.games.find((g) => coverSrc(ENRICH[g._k], "cover_small")) || s.games[0];
  const cs = art ? coverSrc(ENRICH[art._k], "cover_big") : "";
  const complete = s.done === s.games.length;
  return `<button class="fr-card${complete ? " done" : ""}" data-fr="${escapeHtml(s.name)}" data-fk="${escapeHtml(String((art && art._k) || ""))}">
    ${cs ? `<img class="fr-art" loading="lazy" src="${escapeHtml(cs)}" alt="">` : `<span class="fr-art ph">🎮</span>`}
    <span class="fr-body">
      <b>${escapeHtml(s.name)}</b>
      <span class="muted">${s.done} of ${s.games.length} finished${s.owned ? ` · ${s.owned} owned` : ""}</span>
      <span class="ch-bar"><span style="width:${(s.pct * 100).toFixed(1)}%"></span></span>
    </span>
    ${complete ? `<span class="fr-done">✓</span>` : ""}
  </button>`;
}

function groupGameRow(g, i) {
  const cs = coverSrc(ENRICH[g._k], "cover_small");
  const state = g.completed ? "done" : g.playingStatus ? "playing" : g.owned ? "owned" : "missing";
  const mark = { done: "✓", playing: "▶", owned: "•", missing: "" }[state];
  const bits = [g.platform, g.releaseYear].filter(Boolean).map((x) => escapeHtml(String(x))).join(" · ");
  const score = g.rating != null
    ? `<span class="${ratingClass(g.rating)}">${Math.round(g.rating * 100)}</span>` : "";
  const t = playtimeOf(g);
  return `<button class="fr-game fr-${state}" data-fg="${escapeHtml(String(g._k || ""))}" data-fi="${i}">
    <span class="fr-mark">${mark}</span>
    ${cs ? `<img loading="lazy" src="${escapeHtml(cs)}" alt="">` : `<span class="fr-ph">🎮</span>`}
    <span class="fr-game-t"><b>${escapeHtml(String(g.title))}</b><span class="muted">${bits}</span></span>
    <span class="fr-game-x">${t != null ? `<span class="muted">${fmtHours(t)}</span>` : ""}${score}</span>
  </button>`;
}

// ---- render --------------------------------------------------------------

function renderGroups() {
  const host = $("#groups");
  if (!DATA) return;

  if (!groupState.kind) return renderGroupMenu(host);

  const g = grouping(groupState.kind);
  if (!g) { groupState.kind = null; return renderGroups(); }
  const all = grGroups(g.id);

  // ---- one group ----
  if (groupState.open) {
    const s = all.find((x) => x.name === groupState.open) ||
      groupIndex(g.id).find((x) => x.name === groupState.open);
    if (!s) { groupState.open = null; return renderGroups(); }
    host.innerHTML =
      `<div class="fr-detail">
        <button class="ch-back" id="grBack">← All ${escapeHtml(g.units)}</button>
        <div class="fr-head">
          <div>
            <span class="h-eyebrow">${g.icon} ${escapeHtml(g.label)}</span>
            <h1>${escapeHtml(s.name)}</h1>
            <p class="muted">${s.games.length} games · ${s.done} finished · ${s.owned} owned</p>
            <div class="ch-bar big" style="margin:14px 0 0"><span style="width:${(s.pct * 100).toFixed(1)}%"></span></div>
          </div>
          ${typeof chRing === "function" ? chRing(s.pct, 84) : ""}
        </div>
        ${s.next ? `<div class="fr-next">
          <span class="h-eyebrow">Play next</span>
          ${groupGameRow(s.next, s.games.indexOf(s.next))}
        </div>` : ""}
        <h2 class="ch-sec">In release order</h2>
        <div class="fr-games">${s.games.map((x, i) => groupGameRow(x, i)).join("")}</div>
      </div>`;
    $("#grBack").onclick = () => { groupState.open = null; renderGroups(); nav(); };
    host.querySelectorAll("[data-fg]").forEach((el) => {
      el.onclick = () => { const x = s.games[+el.dataset.fi]; if (x) openDrawer(x, "games"); };
    });
    host.scrollTop = 0;
    return;
  }

  // ---- the index for one axis ----
  const q = groupState.q.toLowerCase().trim();
  const sorted = (list) => list.slice().sort((GROUP_SORTS[groupState.sort] || GROUP_SORTS.size).cmp);
  const shown = sorted(all.filter((s) => !q || s.name.toLowerCase().includes(q)));
  const finished = all.filter((s) => s.done === s.games.length).length;

  host.innerHTML =
    `<div class="fr-index-head">
      <button class="ch-back" id="grHome">← All groupings</button>
      <h1>${g.icon} ${escapeHtml(g.label)}</h1>
      <p>${all.length.toLocaleString()} ${escapeHtml(g.units)} with ${MIN_GROUP}+ games · ${finished.toLocaleString()} finished end to end.</p>
      <div class="rev-controls">
        <input id="grQ" type="search" placeholder="Find a ${escapeHtml(g.units.replace(/e?s$/, ""))}…" value="${escapeHtml(groupState.q)}" autocomplete="off">
        <label class="ctl">Sort
          <select id="grSort">${Object.entries(GROUP_SORTS).map(([k, v]) =>
            `<option value="${k}"${k === groupState.sort ? " selected" : ""}>${escapeHtml(v.label)}</option>`).join("")}</select>
        </label>
        <span class="muted">${shown.length.toLocaleString()} shown</span>
      </div>
    </div>
    <div class="fr-grid">${shown.slice(0, 120).map(groupCardHtml).join("")}</div>` +
    (shown.length > 120 ? `<p class="hz-more">Showing the first 120 of ${shown.length.toLocaleString()}.</p>` : "");

  $("#grHome").onclick = () => { groupState.kind = null; groupState.q = ""; renderGroups(); nav(); };
  const qi = $("#grQ");
  qi.oninput = () => {
    // Repaint the grid only — replacing the input would drop focus mid-word.
    groupState.q = qi.value;
    const q2 = groupState.q.toLowerCase().trim();
    $(".fr-grid").innerHTML = sorted(all.filter((s) => !q2 || s.name.toLowerCase().includes(q2)))
      .slice(0, 120).map(groupCardHtml).join("");
    wireGroupCards(host);
  };
  $("#grSort").onchange = (e) => { groupState.sort = e.target.value; renderGroups(); };
  wireGroupCards(host);
}

// The landing page: choose an axis. Each tile shows how many groups that axis
// yields and names the biggest few, so the choice isn't blind.
function renderGroupMenu(host) {
  const tiles = GROUPINGS.map((g) => {
    const all = grGroups(g.id);
    const top = all.slice().sort((a, b) => b.games.length - a.games.length).slice(0, 3);
    const done = all.filter((s) => s.done === s.games.length).length;
    return `<button class="gr-tile" data-gk="${escapeHtml(g.id)}">
      <span class="gr-icon">${g.icon}</span>
      <span class="gr-tile-b">
        <b>${escapeHtml(g.label)}</b>
        <span class="gr-n">${all.length.toLocaleString()} ${escapeHtml(g.units)}${done ? ` · ${done.toLocaleString()} finished` : ""}</span>
        <span class="muted">${escapeHtml(g.blurb)}</span>
        <span class="gr-eg">${top.map((s) =>
          `<span>${escapeHtml(s.name)} <i>${s.games.length}</i></span>`).join("")}</span>
      </span>
      <span class="gr-go">→</span>
    </button>`;
  }).join("");

  host.innerHTML =
    `<div class="fr-index-head">
      <h1>Groupings</h1>
      <p>Your collection, cut along whichever axis you care about. Each one shows what you own,
         what you've finished, how far through you are, and what to play next.</p>
    </div>
    <div class="gr-tiles">${tiles}</div>`;

  host.querySelectorAll("[data-gk]").forEach((el) => {
    el.onclick = () => { groupState.kind = el.dataset.gk; groupState.open = null; groupState.q = ""; renderGroups(); nav(); };
  });
}

function wireGroupCards(host) {
  host.querySelectorAll("[data-fr]").forEach((el) => {
    el.onclick = () => { groupState.open = el.dataset.fr; renderGroups(); nav(); };
  });
}

// Enrichment lands after the first paint; fill the art in place rather than
// re-rendering the grid.
function patchGroupCovers() {
  const host = $("#groups");
  if (!host || host.hidden) return;
  host.querySelectorAll("[data-fk]").forEach((el) => {
    const ph = el.querySelector(".fr-art.ph");
    if (!ph) return;
    const cs = coverSrc(ENRICH[el.dataset.fk], "cover_big");
    if (!cs) return;
    const img = document.createElement("img");
    img.className = "fr-art"; img.loading = "lazy"; img.alt = ""; img.src = cs;
    ph.replaceWith(img);
  });
  host.querySelectorAll("[data-fg]").forEach((el) => {
    const ph = el.querySelector(".fr-ph");
    if (!ph) return;
    const cs = coverSrc(ENRICH[el.dataset.fg], "cover_small");
    if (!cs) return;
    const img = document.createElement("img");
    img.loading = "lazy"; img.alt = ""; img.src = cs;
    ph.replaceWith(img);
  });
}

// Open a grouping from anywhere (a chip in the drawer, the command palette).
function openGroup(kind, name) {
  groupState.kind = kind;
  groupState.open = name || null;
  groupState.q = "";
  switchTab("groups");
  nav();
}
