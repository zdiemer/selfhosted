"use strict";

/* Series — a page per franchise.

   You have 461 franchises with five or more games in them, and no way to see one
   as a whole. This turns "I've been meaning to work through Castlevania" into a
   view: every game in release order, what you own, what you've beaten, how far
   through you are, and the obvious next one to play.

   Loaded after app.js; shares its globals (DATA, ENRICH, openDrawer, …). */

const seriesState = { open: null, q: "", sort: "size" };

const frRows = () => ((DATA.sheets.games || {}).rows || []).filter((r) => r.title && r.franchise);

let _seriesIndex = null;
const resetSeries = () => { _seriesIndex = null; };

function seriesIndex() {
  if (_seriesIndex) return _seriesIndex;
  const m = new Map();
  for (const r of frRows()) {
    if (!m.has(r.franchise)) m.set(r.franchise, []);
    m.get(r.franchise).push(r);
  }
  const out = [];
  for (const [name, games] of m) {
    // Release order is how a series is meant to be read.
    games.sort((a, b) => String(a.releaseDate || a.releaseYear || "").localeCompare(
      String(b.releaseDate || b.releaseYear || "")));
    const done = games.filter((g) => g.completed).length;
    const owned = games.filter((g) => g.owned).length;
    // What to play next: the earliest unfinished game you can actually play.
    const next = games.find((g) => !g.completed && (typeof isCandidate === "function" ? isCandidate(g) : g.owned))
      || games.find((g) => !g.completed);
    out.push({ name, games, done, owned, next, pct: games.length ? done / games.length : 0 });
  }
  return (_seriesIndex = out);
}

const SERIES_SORTS = {
  size: { label: "Most games", cmp: (a, b) => b.games.length - a.games.length },
  progress: { label: "Closest to done", cmp: (a, b) => (b.pct - a.pct) || (b.done - a.done) },
  done: { label: "Most finished", cmp: (a, b) => b.done - a.done },
  name: { label: "A–Z", cmp: (a, b) => a.name.localeCompare(b.name) },
  untouched: { label: "Never started", cmp: (a, b) => (a.pct - b.pct) || (b.games.length - a.games.length) },
};

const MIN_SERIES = 2;      // one game isn't a series

function seriesCardHtml(s) {
  // The cover of whichever game in the series has one — a series has no art of
  // its own, so it borrows from its members.
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

function seriesGameRow(g, i) {
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

function renderSeries() {
  const host = $("#series");
  if (!DATA) return;
  const all = seriesIndex().filter((s) => s.games.length >= MIN_SERIES);

  // ---- one series ----
  if (seriesState.open) {
    const s = seriesIndex().find((x) => x.name === seriesState.open);
    if (!s) { seriesState.open = null; return renderSeries(); }
    const hero = s.games.find((g) => (ENRICH[g._k] || {}).cover);
    const bg = hero ? IMG((DETAIL[hero._k] || {}).artworks?.[0] || "", "1080p") : "";
    host.innerHTML =
      `<div class="fr-detail">
        <button class="ch-back" id="frBack">← All series</button>
        <div class="fr-head">
          <div>
            <h1>${escapeHtml(s.name)}</h1>
            <p class="muted">${s.games.length} games · ${s.done} finished · ${s.owned} owned</p>
            <div class="ch-bar big" style="margin:14px 0 0"><span style="width:${(s.pct * 100).toFixed(1)}%"></span></div>
          </div>
          ${chRing ? chRing(s.pct, 84) : ""}
        </div>
        ${s.next ? `<div class="fr-next">
          <span class="h-eyebrow">Play next</span>
          ${seriesGameRow(s.next, s.games.indexOf(s.next))}
        </div>` : ""}
        <h2 class="ch-sec">In release order</h2>
        <div class="fr-games">${s.games.map((g, i) => seriesGameRow(g, i)).join("")}</div>
      </div>`;
    $("#frBack").onclick = () => { seriesState.open = null; renderSeries(); nav(); };
    host.querySelectorAll("[data-fg]").forEach((el) => {
      el.onclick = () => {
        const g = s.games[+el.dataset.fi];
        if (g) openDrawer(g, "games");
      };
    });
    host.scrollTop = 0;
    return;
  }

  // ---- the index ----
  const q = seriesState.q.toLowerCase().trim();
  const shown = all
    .filter((s) => !q || s.name.toLowerCase().includes(q))
    .sort((SERIES_SORTS[seriesState.sort] || SERIES_SORTS.size).cmp);
  const finished = all.filter((s) => s.done === s.games.length).length;

  host.innerHTML =
    `<div class="fr-index-head">
      <h1>Series</h1>
      <p>${all.length.toLocaleString()} franchises with ${MIN_SERIES}+ games · ${finished} finished end to end.</p>
      <div class="rev-controls">
        <input id="frQ" type="search" placeholder="Find a series…" value="${escapeHtml(seriesState.q)}" autocomplete="off">
        <label class="ctl">Sort
          <select id="frSort">${Object.entries(SERIES_SORTS).map(([k, v]) =>
            `<option value="${k}"${k === seriesState.sort ? " selected" : ""}>${escapeHtml(v.label)}</option>`).join("")}</select>
        </label>
        <span class="muted">${shown.length.toLocaleString()} shown</span>
      </div>
    </div>
    <div class="fr-grid">${shown.slice(0, 120).map(seriesCardHtml).join("")}</div>` +
    (shown.length > 120 ? `<p class="hz-more">Showing the first 120 of ${shown.length.toLocaleString()}.</p>` : "");

  const qi = $("#frQ");
  qi.oninput = () => {
    // Repaint the grid only — replacing the input would drop focus mid-word.
    seriesState.q = qi.value;
    const q2 = seriesState.q.toLowerCase().trim();
    const list = all.filter((s) => !q2 || s.name.toLowerCase().includes(q2))
      .sort((SERIES_SORTS[seriesState.sort] || SERIES_SORTS.size).cmp);
    $(".fr-grid").innerHTML = list.slice(0, 120).map(seriesCardHtml).join("");
    wireSeriesCards(host);
  };
  $("#frSort").onchange = (e) => { seriesState.sort = e.target.value; renderSeries(); };
  wireSeriesCards(host);
}

function wireSeriesCards(host) {
  host.querySelectorAll("[data-fr]").forEach((el) => {
    el.onclick = () => { seriesState.open = el.dataset.fr; renderSeries(); nav(); };
  });
}

// Enrichment lands after the first paint; fill the art in place rather than
// re-rendering the grid.
function patchSeriesCovers() {
  const host = $("#series");
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

// Open a franchise from anywhere (the drawer's franchise chip, say).
function openSeries(name) {
  seriesState.open = name;
  switchTab("series");
  nav();
}
