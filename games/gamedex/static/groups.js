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

/* Developer / publisher / franchise / genre are MULTI-valued now: the sheet's one
   value per row is joined with IGDB's many (unified*Vals), so a game files under every
   studio, publisher, franchise, and genre either source knows — a co-developer only
   IGDB lists gets its own shelf, and the umbrella genres (Platformer, RPG) become
   groups the granular sheet genres roll up into. Platform stays single. */
// The IGDB list-valued fields, straight off the enrichment map.
const igdbVals = (r, field) => (ENRICH[r._k] || {})[field] || [];

const GROUPINGS = [
  { id: "series", label: "Series", icon: "i-timeline", vals: (r) => unifiedFranchiseVals(r),
    units: "franchises", blurb: "Franchises in release order. How far through Castlevania are you?" },
  { id: "developer", label: "Developers", icon: "i-edit", vals: (r) => unifiedDevVals(r),
    units: "studios", blurb: "Every studio you own something by — sheet and IGDB — and what you never got round to." },
  { id: "publisher", label: "Publishers", icon: "i-package", vals: (r) => unifiedPubVals(r),
    units: "publishers", blurb: "Who put it on the shelf, rather than who made it." },
  { id: "genre", label: "Genres", icon: "i-target", vals: (r) => unifiedGenreVals(r),
    units: "genres", blurb: "What you actually play, as opposed to what you buy." },
  { id: "platform", label: "Platforms", icon: "i-dice", vals: (r) => (r.platform ? [r.platform] : []),
    units: "platforms", blurb: "A shelf per machine, with how much of it you've finished." },
  // IGDB knows these about nearly everything and the app has never asked. A perspective is
  // the one axis a genre can't express — plenty of games are "Action" and the only real
  // difference is where the camera sits.
  { id: "keyword", label: "Keywords", icon: "i-star", vals: (r) => igdbVals(r, "keywords"),
    units: "keywords", blurb: "IGDB's finest-grained vocabulary — metroidvania, soulslike, cozy, story rich." },
  { id: "perspective", label: "Perspective", icon: "i-target", vals: (r) => igdbVals(r, "perspectives"),
    units: "perspectives", blurb: "Where the camera sits. First person, side view, isometric — the thing genre never says." },
  { id: "engine", label: "Engines", icon: "i-package", vals: (r) => igdbVals(r, "engines"),
    units: "engines", blurb: "What it was built in. Everything you own running on Unreal, or on Godot." },
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
    for (const k of g.vals(r)) {              // a game can belong to several groups now
      if (!k) continue;
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(r);
    }
  }
  const out = [];
  const mean = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);
  for (const [name, games] of m) {
    // Release order is how any of these is meant to be read.
    games.sort((a, b) => String(a.releaseDate || a.releaseYear || "").localeCompare(
      String(b.releaseDate || b.releaseYear || "")));
    const done = games.filter((x) => x.completed).length;
    const owned = games.filter((x) => x.owned).length;
    // What to play next: the earliest unfinished game you can actually play.
    const next = games.find((x) => !x.completed && (typeof isCandidate === "function" ? isCandidate(x) : x.owned))
      || games.find((x) => !x.completed);

    /* What a group is actually WORTH knowing, aggregated over its members: how you rate
       it against the critics, the hours it has already taken and the hours still in it,
       what you spent, and the span it covers. All null-safe — a group where nothing is
       rated simply doesn't show a rating. */
    const rated = games.map((x) => x.rating).filter((v) => v != null);
    const crits = games.map((x) => (typeof criticOf === "function" ? criticOf(x) : null))
      .filter((v) => v != null);
    const played = games.reduce(
      (s, x) => s + (x.completed && x.completionTime ? +x.completionTime : 0), 0);
    const left = games.filter((x) => !x.completed).reduce((s, x) => {
      const t = typeof playtimeOf === "function" ? playtimeOf(x) : null;
      return s + (t != null ? t : 0);
    }, 0);
    const spent = games.reduce((s, x) => s + (x.purchasePrice ? +x.purchasePrice : 0), 0);
    const yrs = games.map((x) => +x.releaseYear).filter((y) => y > 1900);

    out.push({
      name, games, done, owned, next, pct: games.length ? done / games.length : 0,
      avgRating: mean(rated), nRated: rated.length,
      avgCritic: mean(crits),
      played, left, spent,
      y0: yrs.length ? Math.min(...yrs) : null,
      y1: yrs.length ? Math.max(...yrs) : null,
    });
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
  // Now that a group carries its own numbers, they're worth sorting on: which studio do
  // I actually rate highest, which series has eaten the most of my life.
  rating: { label: "Best rated", cmp: (a, b) => (b.avgRating ?? -1) - (a.avgRating ?? -1) },
  played: { label: "Most played", cmp: (a, b) => (b.played || 0) - (a.played || 0) },
};

// ---- aggregates ----------------------------------------------------------

// The group's numbers, as the same stat strip the game drawer uses.
function groupStatsHtml(s) {
  const pct = (v) => String(Math.round(v * 100));
  const cells = [];
  if (s.avgRating != null) cells.push([pct(s.avgRating), `Your rating · ${s.nRated} rated`, ratingClass(s.avgRating)]);
  if (s.avgCritic != null) cells.push([pct(s.avgCritic), "Critics", ratingClass(s.avgCritic)]);
  if (s.played > 0) cells.push([fmtHours(s.played), "You've played", ""]);
  if (s.left > 0) cells.push([fmtHours(s.left), "Left to beat", ""]);
  if (s.spent > 0) cells.push(["$" + Math.round(s.spent), "Spent", ""]);
  if (s.y0) cells.push([s.y0 === s.y1 ? String(s.y0) : `${s.y0}–${s.y1}`, "Span", ""]);
  if (!cells.length) return "";
  return `<div class="hero-stats fr-stats">` + cells.map(([v, l, cls]) =>
    `<div class="hero-stat"><b class="${cls}">${escapeHtml(String(v))}</b><span>${escapeHtml(l)}</span></div>`
  ).join("") + `</div>`;
}

// One line of it for the index card — the two that tell you most at a glance.
function groupCardStats(s) {
  const bits = [];
  if (s.avgRating != null) bits.push(`<span class="${ratingClass(s.avgRating)}">${Math.round(s.avgRating * 100)}%</span> avg`);
  if (s.played > 0) bits.push(`${fmtHours(s.played)} played`);
  else if (s.left > 0) bits.push(`${fmtHours(s.left)} left`);
  return bits.length ? `<span class="muted fr-mini">${bits.join(" · ")}</span>` : "";
}

// ---- cards ---------------------------------------------------------------

function groupCardHtml(s) {
  // A group has no art of its own, so it borrows from whichever member has some.
  const art = s.games.find((g) => coverSrc(ENRICH[g._k], "cover_small")) || s.games[0];
  const cs = art ? coverSrc(ENRICH[art._k], "cover_big") : "";
  const complete = s.done === s.games.length;
  return `<button class="fr-card${complete ? " done" : ""}" data-fr="${escapeHtml(s.name)}" data-fk="${escapeHtml(String((art && art._k) || ""))}">
    ${cs ? `<img class="fr-art" loading="lazy" src="${escapeHtml(cs)}" alt="">` : `<span class="fr-art ph">${icon("i-library", 22)}</span>`}
    <span class="fr-body">
      <b>${escapeHtml(s.name)}</b>
      <span class="muted">${s.done} of ${s.games.length} finished${s.owned ? ` · ${s.owned} owned` : ""}</span>
      ${groupCardStats(s)}
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
    ${cs ? `<img loading="lazy" src="${escapeHtml(cs)}" alt="">` : `<span class="fr-ph">${icon("i-library", 16)}</span>`}
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
            <span class="h-eyebrow">${glyph(g.icon, 14)} ${escapeHtml(g.label)}</span>
            <h1>${escapeHtml(s.name)}</h1>
            <p class="muted">${s.games.length} games · ${s.done} finished · ${s.owned} owned</p>
            <div class="ch-bar big" style="margin:14px 0 0"><span style="width:${(s.pct * 100).toFixed(1)}%"></span></div>
          </div>
          ${typeof chRing === "function" ? chRing(s.pct, 84) : ""}
        </div>
        ${groupStatsHtml(s)}
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
      <h1>${glyph(g.icon, 24)} ${escapeHtml(g.label)}</h1>
      <p>${all.length.toLocaleString()} ${escapeHtml(g.units)} with ${MIN_GROUP}+ games · ${finished.toLocaleString()} finished end to end.</p>
      <div class="rev-controls">
        ${searchField("grQ", `Find a ${g.units.replace(/e?s$/, "")}…`, groupState.q)}
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
      <span class="gr-icon">${glyph(g.icon, 26)}</span>
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
