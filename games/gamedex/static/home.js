"use strict";

/* The landing page.

   Everything here is derived from the sheet + the enrichment cache — there's no
   new data source. The point is to answer "what should I do right now?" without
   making you search 14.7k rows for it.

   Loaded after app.js/challenges.js; shares their globals (DATA, ENRICH,
   openDrawer, isCandidate, combinedRating, playtimeOf, …). */

const homeState = { heroIdx: 0 };
let _homeTimer = null;

const hRows = () => ((DATA.sheets.games || {}).rows || []).filter((r) => r.title);
const hCompleted = () => ((DATA.sheets.completed || {}).rows || []);
const hOrders = () => ((DATA.sheets.onOrder || {}).rows || []);

const byStatus = (s) => hRows().filter((r) => r.playingStatus === s);
const hToday = () => new Date();
const hMD = (d) => `-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const yearsAgo = (iso) => hToday().getFullYear() - +String(iso).slice(0, 4);
const agoText = (n) => (n <= 0 ? "today" : n === 1 ? "1 year ago" : `${n} years ago`);

// Newest first by whichever date field is meaningful for the row.
const byDateDesc = (field) => (a, b) => String(b[field] || "").localeCompare(String(a[field] || ""));

// ---- suggestions ---------------------------------------------------------
// Each rule inspects a candidate and, if it applies, returns the reason it's
// worth playing. The first rule that fires wins, so they're ordered by how
// *interesting* the reason is, not by how common it is — "the last game you need
// for a challenge" beats "it's short", even though both are true.

let _homeFranchiseCount = null;
function franchiseCounts() {
  if (_homeFranchiseCount) return _homeFranchiseCount;
  const m = new Map();
  for (const r of hCompleted()) if (r.franchise) m.set(r.franchise, (m.get(r.franchise) || 0) + 1);
  return (_homeFranchiseCount = m);
}
let _homeGenreAvg = null;
function genreAffinity() {
  if (_homeGenreAvg) return _homeGenreAvg;
  const sum = new Map(), n = new Map();
  for (const r of hCompleted()) {
    if (!r.genre || r.rating == null) continue;
    sum.set(r.genre, (sum.get(r.genre) || 0) + r.rating);
    n.set(r.genre, (n.get(r.genre) || 0) + 1);
  }
  const out = new Map();
  for (const [g, s] of sum) if (n.get(g) >= 5) out.set(g, s / n.get(g));
  return (_homeGenreAvg = out);
}

const SUGGESTION_RULES = [
  {
    id: "franchise",
    test: (r) => {
      const n = franchiseCounts().get(r.franchise) || 0;
      return n >= 3 ? `You’ve beaten ${n} ${r.franchise} games` : null;
    },
  },
  {
    id: "tonight",
    test: (r) => {
      const t = playtimeOf(r), m = metacriticOf(r);
      return t != null && t <= 3 && m != null && m >= 0.8
        ? `${fmtHours(t)} and rated ${Math.round(m * 100)} — finish it tonight` : null;
    },
  },
  {
    id: "shelved",
    test: (r) => {
      if (!r.datePurchased || r.dateStarted) return null;
      const y = yearsAgo(r.datePurchased);
      return y >= 3 ? `Bought ${agoText(y)} and never started` : null;
    },
  },
  {
    id: "acclaimed",
    test: (r) => {
      const m = metacriticOf(r);
      return m != null && m >= 0.9 ? `Metacritic ${Math.round(m * 100)} — one of the best you own` : null;
    },
  },
  {
    id: "genre",
    test: (r) => {
      const a = genreAffinity().get(r.genre);
      return a != null && a >= 0.8
        ? `You rate ${r.genre} ${Math.round(a * 100)}% on average` : null;
    },
  },
  {
    id: "priority",
    test: (r) => (Number(r.priority) >= 4 ? `Flagged high priority` : null),
  },
  {
    id: "wishlist",
    test: (r) => (r.wishlisted ? `Still on your wishlist` : null),
  },
  {
    id: "backlog",
    test: (r) => {
      const t = playtimeOf(r);
      return t != null && t <= 8 ? `A short one — about ${fmtHours(t)}` : null;
    },
  },
];

function suggestions(n = 6) {
  const pool = hRows().filter((r) => {
    if (r.completed || r.playingStatus) return false;
    return typeof isCandidate === "function" ? isCandidate(r) : !!r.owned;
  });
  // Best-rated first, then take at most two per rule so the reasons stay varied.
  const scored = pool
    .map((r) => ({ r, s: combinedRating(r) ?? 0 }))
    .sort((a, b) => b.s - a.s);
  const used = new Map(), out = [];
  for (const { r } of scored) {
    if (out.length >= n) break;
    for (const rule of SUGGESTION_RULES) {
      const why = rule.test(r);
      if (!why) continue;
      const k = used.get(rule.id) || 0;
      if (k >= 2) break;                       // don't let one rule fill the row
      used.set(rule.id, k + 1);
      out.push({ row: r, why, rule: rule.id });
      break;
    }
  }
  return out;
}

// ---- pieces --------------------------------------------------------------

function homeCard(row, sheet, note) {
  const cs = coverSrc(ENRICH[row._k], "cover_big");
  const pixel = coverIsPixelArt(ENRICH[row._k], cs) ? " pixel" : "";
  const title = escapeHtml(String(row.title || row.game || "Untitled"));
  const cover = cs
    ? `<img class="card-cover${pixel}" loading="lazy" src="${escapeHtml(cs)}" alt="">`
    : `<div class="card-cover ph">${icon("i-library", 26)}</div>`;
  const sub = [row.platform, row.releaseYear].filter((x) => x != null && x !== "")
    .map((x) => escapeHtml(String(x))).join(" · ");
  return `<button class="card h-card" data-hk="${escapeHtml(String(row._k || ""))}" data-hs="${sheet}">
    ${cover}
    <div class="card-body">
      <div class="card-title">${title}</div>
      <div class="card-sub">${sub}</div>
      ${note ? `<div class="h-note">${note}</div>` : ""}
    </div></button>`;
}

// A horizontally-scrolling shelf with arrows.
function shelf(id, title, cards, action) {
  if (!cards.length) return "";
  return `<section class="h-sect">
    <div class="h-sect-head">
      <h2>${title}</h2>
      <div class="h-sect-act">
        ${action || ""}
        <button class="h-arrow" data-scroll="${id}" data-dir="-1" aria-label="Scroll left">‹</button>
        <button class="h-arrow" data-scroll="${id}" data-dir="1" aria-label="Scroll right">›</button>
      </div>
    </div>
    <div class="h-shelf" id="${id}">${cards.join("")}</div>
  </section>`;
}

// The big one: whatever you're actually in the middle of.
function heroSection(playing) {
  if (!playing.length) return "";
  const row = playing[homeState.heroIdx % playing.length];
  const e = ENRICH[row._k] || {};
  const cs = coverSrc(e, "cover_big");
  const shot = (DETAIL[row._k] || {}).screenshots || [];
  const bg = shot.length ? IMG(shot[0], "screenshot_big") : "";
  const prog = row.playingProgress != null ? Math.round(+row.playingProgress * 100) : null;
  const t = playtimeOf(row);
  const left = (prog != null && t != null) ? t * (1 - prog / 100) : null;
  const bits = [row.platform, row.genre].filter(Boolean).map((x) => escapeHtml(String(x))).join(" · ");
  const dots = playing.length > 1
    ? `<div class="h-dots">${playing.map((_, i) =>
        `<button class="h-dot${i === homeState.heroIdx % playing.length ? " on" : ""}" data-hero="${i}" aria-label="Game ${i + 1}"></button>`).join("")}</div>`
    : "";
  const pager = playing.length > 1
    ? `<button class="h-page prev" data-page="-1" aria-label="Previous game">‹</button>
       <button class="h-page next" data-page="1" aria-label="Next game">›</button>` : "";
  return `<section class="h-hero" style="${bg ? `--shot:url('${bg}')` : ""}">
    <div class="h-hero-bg${bg ? " on" : ""}"></div>
    ${pager}
    <div class="h-hero-inner">
      ${cs ? `<img class="h-hero-cover" src="${escapeHtml(cs)}" alt="">` : `<div class="h-hero-cover ph">${icon("i-library", 34)}</div>`}
      <div class="h-hero-txt">
        <span class="h-eyebrow">Continue playing</span>
        <h1>${escapeHtml(String(row.title))}</h1>
        <div class="h-hero-meta">${bits}${row.dateStarted ? ` · started ${escapeHtml(fmtDate(row.dateStarted))}` : ""}</div>
        ${prog != null ? `<div class="h-prog"><span style="width:${prog}%"></span></div>
          <div class="h-prog-txt">${prog}% through${left != null ? ` · about ${fmtHours(left)} left` : ""}</div>` : ""}
        <span class="h-actions">
          ${launchHtml(row)}
          <button class="btn ghost h-open" data-hk="${escapeHtml(String(row._k || ""))}" data-hs="games">Details</button>
        </span>
      </div>
      ${dots}
    </div>
  </section>`;
}

// Releases and completions that share today's calendar date.
function onThisDay() {
  const md = hMD(hToday());
  const rel = hRows()
    .filter((r) => typeof r.releaseDate === "string" && r.releaseDate.slice(4) === md)
    .sort((a, b) => (combinedRating(b) ?? 0) - (combinedRating(a) ?? 0));
  const done = hCompleted()
    .filter((r) => typeof r.date === "string" && r.date.slice(4) === md)
    .sort(byDateDesc("date"));
  if (!rel.length && !done.length) return "";

  const relCards = rel.slice(0, 12).map((r) =>
    homeCard(r, "games", `Released ${agoText(yearsAgo(r.releaseDate))}`));
  const doneCards = done.slice(0, 12).map((r) =>
    homeCard(r, "completed", `You finished it ${agoText(yearsAgo(r.date))}`));

  const today = hToday().toLocaleDateString(undefined, { month: "long", day: "numeric" });
  return `<div class="h-otd-head"><h2>${icon("i-calendar", 17)} On this day <span class="muted">${escapeHtml(today)}</span></h2></div>` +
    shelf("otdDone", `<span class="h-sub">You finished these</span>`, doneCards) +
    shelf("otdRel", `<span class="h-sub">Released on this date</span>`, relCards);
}

// The challenge you're closest to finishing.
function challengeSpotlight() {
  if (typeof CHALLENGES === "undefined") return "";
  const live = CHALLENGES.map(computeChallenge).filter((r) => r.total && r.remaining.size);
  if (!live.length) return "";
  live.sort((a, b) => a.remaining.size - b.remaining.size);
  const r = live[0];
  const buckets = chSortBuckets(r, r.remaining).slice(0, 4);
  return `<section class="h-sect">
    <div class="h-sect-head"><h2>${icon("i-target", 17)} Closest challenge</h2>
      <div class="h-sect-act"><button class="linkbtn" id="hChalAll">See all →</button></div></div>
    <button class="h-chal" id="hChal">
      <span class="ch-icon big">${r.c.icon}</span>
      <span class="h-chal-txt">
        <b>${escapeHtml(r.c.name)}</b>
        <span class="muted">${r.cleared.size} of ${r.total} cleared — ${r.remaining.size} to go</span>
        <span class="ch-bar"><span style="width:${(r.pct * 100).toFixed(1)}%"></span></span>
        <span class="h-chal-left">Left: ${buckets.map(([k]) => escapeHtml(String(k))).join(" · ")}${r.remaining.size > 4 ? " …" : ""}</span>
      </span>
    </button>
  </section>`;
}

// ---- render --------------------------------------------------------------

// Swap just the hero, leaving every other <img> on the page untouched.
function renderHero(playing) {
  const cur = document.querySelector(".h-hero");
  if (!cur) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = heroSection(playing);
  const next = tmp.firstElementChild;
  if (!next) return;
  cur.replaceWith(next);
  wireHeroBits(next, playing);
}

// Enrichment arrived: fill in the covers that were placeholders, in place.
// A full renderHome() here would recreate every <img> and flash the whole page.
function patchHomeCovers() {
  const host = $("#home");
  if (!host) return;
  host.querySelectorAll("[data-hk]").forEach((el) => {
    const ph = el.querySelector(".card-cover.ph");
    if (!ph) return;
    const e = ENRICH[el.dataset.hk];
    const cs = coverSrc(e, "cover_big");
    if (!cs) return;
    const img = document.createElement("img");
    img.className = "card-cover" + (coverIsPixelArt(e, cs) ? " pixel" : "");
    img.loading = "lazy"; img.alt = ""; img.src = cs;
    ph.replaceWith(img);
  });
}

function renderHome() {
  const host = $("#home");
  if (!DATA) return;
  _homeFranchiseCount = null; _homeGenreAvg = null;

  const playing = byStatus("Playing").sort(byDateDesc("dateStarted"));
  const upNext = byStatus("Up Next");
  const onHold = byStatus("On Hold");
  const added = hRows().filter((r) => r.dateAdded && !r.completed).sort(byDateDesc("dateAdded")).slice(0, 18);
  const recent = hCompleted().slice().sort(byDateDesc("date")).slice(0, 18);
  // Every order is estimatedRelease "N/A" / status "Pending" — neither says
  // anything. What's actually informative is when you ordered it and from whom.
  const orders = hOrders().slice().sort(byDateDesc("orderedDate")).slice(0, 18);
  const picks = suggestions(8);

  // Recommendations come from the server (IGDB's similar-games, crossed with
  // your backlog); predictions are computed here from your own ratings.
  const recRows = (RECS || []).map((rec) => {
    const row = hRows().find((r) => String(r._k || "") === rec.key);
    return row ? { row, rec } : null;
  }).filter(Boolean).slice(0, 18);

  const loved = hRows()
    .filter((r) => !r.completed && !r.playingStatus && (typeof isCandidate !== "function" || isCandidate(r)))
    .map((r) => ({ r, p: typeof predictedCached === "function" ? predictedCached(r) : null }))
    .filter((x) => x.p && x.p.confidence >= 0.75)
    .sort((a, b) => b.p.score - a.p.score)
    .slice(0, 18);

  host.innerHTML =
    heroSection(playing) +
    (picks.length ? `<section class="h-sect">
      <div class="h-sect-head"><h2>✨ Picked for you</h2>
        <div class="h-sect-act"><button class="linkbtn" id="hPickMore">Roll one instead →</button></div></div>
      <div class="h-picks">${picks.map((p) =>
        homeCard(p.row, "games", `<span class="h-why">${escapeHtml(p.why)}</span>`)).join("")}</div>
    </section>` : "") +
    shelf("hRecs", `${icon("i-star", 16)} Because you liked…`, recRows.map(({ row, rec }) =>
      homeCard(row, "games", `<span class="h-why">Like ${escapeHtml(rec.because.slice(0, 2).join(" & "))}</span>`))) +
    shelf("hLoved", `${icon("i-trend", 16)} You'd probably love`, loved.map(({ r, p }) =>
      homeCard(r, "games", `<span class="h-why">~${Math.round(p.score * 100)}% predicted</span>`))) +
    shelf("hPlaying", `${icon("i-play", 16)} Now playing`, playing.map((r) => homeCard(r, "games",
      r.playingProgress != null ? `${Math.round(+r.playingProgress * 100)}% through` : ""))) +
    shelf("hNext", `${icon("i-play", 16)} Up next`, upNext.map((r) => homeCard(r, "games"))) +
    shelf("hHold", `${icon("i-clock", 16)} On hold`, onHold.map((r) => homeCard(r, "games",
      r.dateStarted ? `Started ${escapeHtml(fmtDate(r.dateStarted))}` : ""))) +
    onThisDay() +
    shelf("hRecent", `${icon("i-trophy", 16)} Recently finished`, recent.map((r) => homeCard(r, "completed",
      r.rating != null ? `You gave it ${Math.round(r.rating * 100)}%` : (r.date ? escapeHtml(fmtDate(r.date)) : "")))) +
    shelf("hAdded", `${icon("i-plus", 16)} Recently added`, added.map((r) => homeCard(r, "games",
      r.dateAdded ? `Added ${escapeHtml(fmtDate(r.dateAdded))}` : ""))) +
    shelf("hOrder", `${icon("i-package", 16)} On order`, orders.map((r) => homeCard(r, "onOrder",
      [r.orderedDate ? `Ordered ${escapeHtml(fmtDate(r.orderedDate))}` : "", r.vendor ? escapeHtml(String(r.vendor)) : ""]
        .filter(Boolean).join(" · ")))) +
    challengeSpotlight();

  wireHome(host, playing);
}

// Click handlers for the hero's own buttons (cover/open/dots).
// Swipe the hero on touch devices. A 7px dot is not a tap target, and paging a
// carousel by poking dots is the wrong gesture on a phone anyway.
function wireHeroSwipe(scope, playing) {
  if (playing.length < 2) return;
  let x0 = null, y0 = null;
  scope.addEventListener("touchstart", (e) => {
    x0 = e.changedTouches[0].clientX; y0 = e.changedTouches[0].clientY;
  }, { passive: true });
  scope.addEventListener("touchend", (e) => {
    if (x0 == null) return;
    const dx = e.changedTouches[0].clientX - x0;
    const dy = e.changedTouches[0].clientY - y0;
    x0 = null;
    if (Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy)) return;   // a scroll, not a swipe
    const n = playing.length;
    homeState.heroIdx = (homeState.heroIdx + (dx < 0 ? 1 : -1) + n) % n;
    renderHero(playing);
    loadHeroShot(playing);
  }, { passive: true });
}

function wireHeroBits(scope, playing) {
  wireHeroSwipe(scope, playing);
  scope.querySelectorAll(".h-page").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const n = playing.length;
      homeState.heroIdx = (homeState.heroIdx + (+el.dataset.page) + n) % n;
      renderHero(playing);
      loadHeroShot(playing);
    };
  });
  scope.querySelectorAll("[data-hk]").forEach((el) => {
    el.onclick = () => {
      const row = hRows().find((r) => String(r._k || "") === el.dataset.hk);
      if (row) openDrawer(row, "games");
    };
  });
  scope.querySelectorAll(".h-dot").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      homeState.heroIdx = +el.dataset.hero;
      renderHero(playing);
      loadHeroShot(playing);
    };
  });
}

// The hero wants a screenshot backdrop; fetch the detail for the shown game
// once, then redraw only the hero with it.
function loadHeroShot(playing) {
  if (!playing.length || !ENRICH_ENABLED) return;
  const row = playing[homeState.heroIdx % playing.length];
  if (!row._k || DETAIL[row._k]) return;
  fetch("api/enrichment/detail?key=" + encodeURIComponent(row._k))
    .then((r) => r.json())
    .then((j) => {
      if (j.status === "matched" && j.detail && (j.detail.screenshots || []).length) {
        DETAIL[row._k] = j.detail;
        if (activeTab === "home") renderHero(playing);
      }
    })
    .catch(() => {});
}

function wireHome(host, playing) {
  // Any card / hero button opens the game.
  host.querySelectorAll("[data-hk]").forEach((el) => {
    const k = el.dataset.hk, sheetKey = el.dataset.hs;
    const src = sheetKey === "completed" ? hCompleted() : sheetKey === "onOrder" ? hOrders() : hRows();
    const row = src.find((r) => String(r._k || "") === k);
    el.onclick = () => { if (row) openDrawer(row, sheetKey); };
    // Hover-to-play trailers, same as the grid. Home is the tab you land on, so
    // leaving it out meant the feature looked broken to anyone who never left it.
    if (row && el.classList.contains("card")) wirePreviewFor(el, row);
  });
  host.querySelectorAll(".h-arrow").forEach((el) => {
    el.onclick = () => {
      const shelfEl = document.getElementById(el.dataset.scroll);
      if (shelfEl) shelfEl.scrollBy({ left: +el.dataset.dir * shelfEl.clientWidth * 0.8, behavior: "smooth" });
    };
  });
  const more = $("#hPickMore");
  if (more) more.onclick = () => { switchTab("pick"); nav(); };
  const chal = $("#hChal"), chalAll = $("#hChalAll");
  if (chal) chal.onclick = () => { chState.open = null; switchTab("challenges"); nav(); };
  if (chalAll) chalAll.onclick = (e) => { e.stopPropagation(); chState.open = null; switchTab("challenges"); nav(); };

  loadHeroShot(playing);

  // Rotate the hero every 9s, but never while the user is reading a drawer.
  // renderHero, NOT renderHome — the latter rebuilds every <img> on the page.
  clearInterval(_homeTimer);
  if (playing.length > 1 && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    _homeTimer = setInterval(() => {
      if (activeTab !== "home" || !$("#overlay").hidden) return;
      homeState.heroIdx = (homeState.heroIdx + 1) % playing.length;
      renderHero(playing);
      loadHeroShot(playing);
    }, 9000);
  }
}
