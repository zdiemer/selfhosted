/* The shelf — the physical collection, as objects you can pick up.
 *
 * The shelf is SPINES: flat divs with a background colour or a scanned spine. Two
 * thousand games is two thousand divs, and that is all it costs.
 *
 * Exactly ONE 3D case exists at a time — the game in your hand. It is built when you
 * pull a game out and thrown away when you put it back, so a shelf of 2,000 never pays
 * for 2,000 `preserve-3d` subtrees.
 *
 * The pull is one number. At rotateY(90deg) a case is edge-on: the front cover has zero
 * projected width and the only thing you can see is its left wall — which IS a spine.
 * So pulling a game off the shelf is animating that angle from 90 to 24 while the case
 * rises toward you. No swap, no cross-fade, no second element: the spine you clicked is
 * the case, turned.
 */

const SHELF = { games: [], loaded: false, filter: "", plat: "" };

const PX_MM = 1.28;                  // one scale, millimetres -> pixels, for everything
const SHELF_REST_Y = 24;             // +y turns the LEFT wall (the spine) toward you
const SHELF_TILT_MAX = 34;
const SHELF_PULL_Z = 210;
const SHELF_PERSP = 1600;            // must match .sh-stage { perspective }
const SHELF_TOP_GAP = 46;
const SHELF_PERSP_Y = 0.42;  // fraction of the viewport; must match .sh-pull CSS
const shClamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const shLerp = (a, b, t) => a + (b - a) * t;
const shPx = (mm) => Math.round(mm * PX_MM);
// Key goes in the QUERY string, never the path: a match key can contain '/' (Commodore
// Plus/4, OS/2, TI-99/4A), and an encoded slash in a path is decoded by the proxy and
// re-splits the route — a 405 on upload, a 404 on the face.
const faceUrl = (k, f, v) =>
  `/api/shelf/face?key=${encodeURIComponent(k)}&face=${f}${v ? `&v=${v}` : ""}`;

/* A game with no scanned wrap doesn't get a flat coloured slab — it gets that
 * system's STANDARD spine: the console's brand band up top, the game's title set
 * automatically below. Brand colours are facts; the short label stands in for the
 * logo we can't reproduce. Anything not in the table falls back to a neutral spine
 * carrying the platform name, so every game reads as a real object on the shelf. */
// Each entry paints that console's real retail spine as closely as styled text can:
// accurate brand colour, the band where the system puts it, an edge stripe where the
// case has one (the PS2's blue spine, the Xbox green rail), and the wordmark set in
// that family's style. `mark` is extra CSS for the wordmark; `stripe` an outer-edge bar.
const PS_MARK   = "text-transform:none;font-weight:600;letter-spacing:.01em;";
const XBOX_MARK = "font-weight:800;letter-spacing:.18em;";
const WII_MARK  = "text-transform:none;font-weight:800;letter-spacing:.02em;";
const SPINE_LOGOS = {
  gameboy:  { base:"linear-gradient(90deg,#8f88ab,#a49dbf)", band:"#211d38", bandInk:"#e7e3f4", ink:"#1a182a", label:"GAME BOY", mark:"font-size:7.5px;" },
  gbc:      { base:"linear-gradient(90deg,#574fa4,#6a61b6)", band:"#241a66", bandInk:"#ffd23f", ink:"#ffffff", label:"GAME BOY COLOR", mark:"font-size:7px;letter-spacing:.04em;" },
  gba:      { base:"linear-gradient(90deg,#33287a,#40338c)", band:"#160f4a", bandInk:"#9c8ef6", ink:"#ffffff", label:"GAME BOY ADVANCE", mark:"font-size:7px;letter-spacing:.04em;" },
  ds:       { base:"linear-gradient(90deg,#eceef2,#f6f7fa)", band:"#c6c9d1", bandInk:"#16181d", ink:"#16181d", label:"NINTENDO DS", mark:"font-size:7.5px;" },
  n3ds:     { base:"linear-gradient(90deg,#eceef2,#f6f7fa)", band:"#d21a24", bandInk:"#ffffff", ink:"#16181d", label:"NINTENDO 3DS", mark:"font-size:7.5px;" },
  nes:      { base:"linear-gradient(90deg,#bfbfc8,#cdcdd5)", band:"#1c1c1f", bandInk:"#e6352b", ink:"#16181d", label:"NES", mark:"letter-spacing:.12em;" },
  snes:     { base:"linear-gradient(90deg,#c5c6d0,#d2d3db)", band:"#4b3f8f", bandInk:"#ffffff", ink:"#1b1c22", label:"SUPER NINTENDO", mark:"font-size:7px;" },
  n64:      { base:"linear-gradient(90deg,#141416,#202024)", band:"#141416", bandInk:"#f2f2f5", ink:"#f2f2f5", label:"NINTENDO 64", mark:"font-size:7.5px;" },
  gamecube: { base:"linear-gradient(90deg,#352a72,#463a8c)", band:"#1e1650", bandInk:"#c9c2f0", ink:"#ffffff", label:"GAMECUBE" },
  wii:      { base:"linear-gradient(90deg,#eef1f5,#f7f9fb)", band:"#12a5db", bandInk:"#ffffff", ink:"#16181d", label:"Wii", mark:WII_MARK },
  wiiu:     { base:"linear-gradient(90deg,#eef1f5,#f7f9fb)", band:"#0e93cc", bandInk:"#ffffff", ink:"#16181d", label:"Wii U", mark:WII_MARK },
  switch:   { base:"linear-gradient(90deg,#dd0018,#f01b2c)", band:"#c00010", bandInk:"#ffffff", ink:"#ffffff", label:"SWITCH", mark:"letter-spacing:.14em;" },
  switch2:  { base:"linear-gradient(90deg,#d1001a,#e6142c)", band:"#b3000f", bandInk:"#ffffff", ink:"#ffffff", label:"SWITCH 2", mark:"letter-spacing:.1em;" },
  ps1:      { base:"linear-gradient(90deg,#121318,#1c1d24)", band:"#121318", bandInk:"#f2f2f5", ink:"#e9e9ef", label:"PlayStation", mark:PS_MARK },
  ps2:      { base:"linear-gradient(90deg,#0b0b12,#15161e)", band:"#0b0b12", bandInk:"#dfe4ff", ink:"#eef0f6", label:"PlayStation 2", mark:PS_MARK+"font-size:7.5px;", stripe:"#0e39c4" },
  ps3:      { base:"linear-gradient(90deg,#0d0d12,#17171e)", band:"#0d0d12", bandInk:"#e8e8ee", ink:"#eef0f6", label:"PS3", mark:"font-weight:800;" },
  ps4:      { base:"linear-gradient(90deg,#0e1116,#181c24)", band:"#0064d2", bandInk:"#ffffff", ink:"#eef0f6", label:"PS4", mark:"font-weight:800;" },
  ps5:      { base:"linear-gradient(90deg,#f4f6f9,#ffffff)", band:"#101014", bandInk:"#ffffff", ink:"#16181d", label:"PS5", mark:"font-weight:800;" },
  psp:      { base:"linear-gradient(90deg,#101015,#1a1a20)", band:"#101015", bandInk:"#e8e8ee", ink:"#eef0f6", label:"PSP", mark:"font-weight:800;letter-spacing:.1em;" },
  vita:     { base:"linear-gradient(90deg,#101015,#1a1a20)", band:"#101015", bandInk:"#e8e8ee", ink:"#eef0f6", label:"PS VITA", mark:"font-weight:700;letter-spacing:.06em;" },
  xbox:     { base:"linear-gradient(90deg,#107c10,#149314)", band:"#0a5c0a", bandInk:"#eafff0", ink:"#ffffff", label:"XBOX", mark:XBOX_MARK },
  xbox360:  { base:"linear-gradient(90deg,#eef2ee,#f8faf8)", band:"#107c10", bandInk:"#ffffff", ink:"#16181d", label:"XBOX 360", mark:XBOX_MARK },
  xboxone:  { base:"linear-gradient(90deg,#15171b,#1f2228)", band:"#107c10", bandInk:"#ffffff", ink:"#eef0f6", label:"XBOX ONE", mark:XBOX_MARK },
  xboxsx:   { base:"linear-gradient(90deg,#0c0d10,#16181d)", band:"#107c10", bandInk:"#ffffff", ink:"#eef0f6", label:"XBOX SERIES", mark:XBOX_MARK },
  genesis:  { base:"linear-gradient(90deg,#141416,#202024)", band:"#c1121f", bandInk:"#ffffff", ink:"#f2f2f5", label:"GENESIS", mark:"letter-spacing:.1em;" },
  dreamcast:{ base:"linear-gradient(90deg,#eef1f5,#f7f9fb)", band:"#e35205", bandInk:"#ffffff", ink:"#16181d", label:"DREAMCAST", mark:"font-size:7.5px;" },
  saturn:   { base:"linear-gradient(90deg,#101014,#1a1a20)", band:"#101014", bandInk:"#e8e8ee", ink:"#eef0f6", label:"SEGA SATURN", mark:"font-size:7.5px;letter-spacing:.06em;" },
};
const PLAT_KEY = {
  "nintendo switch":"switch", "switch":"switch",
  "nintendo switch 2":"switch2", "switch 2":"switch2",
  "super nintendo entertainment system":"snes", "super nintendo":"snes", "snes":"snes",
  "nintendo entertainment system":"nes", "nes":"nes",
  "nintendo 64":"n64", "n64":"n64",
  "nintendo gamecube":"gamecube", "gamecube":"gamecube",
  "nintendo wii":"wii", "wii":"wii",
  "nintendo wii u":"wiiu", "wii u":"wiiu",
  "nintendo 3ds":"n3ds", "new nintendo 3ds":"n3ds", "3ds":"n3ds",
  "nintendo ds":"ds", "nintendo dsi":"ds", "ds":"ds", "nds":"ds",
  "nintendo game boy":"gameboy", "game boy":"gameboy",
  "nintendo game boy color":"gbc", "game boy color":"gbc",
  "nintendo game boy advance":"gba", "game boy advance":"gba",
  "playstation":"ps1", "playstation 1":"ps1", "ps1":"ps1", "psx":"ps1", "ps one":"ps1",
  "playstation 2":"ps2", "ps2":"ps2",
  "playstation 3":"ps3", "ps3":"ps3",
  "playstation 4":"ps4", "ps4":"ps4",
  "playstation 5":"ps5", "ps5":"ps5",
  "playstation portable":"psp", "psp":"psp",
  "playstation vita":"vita", "ps vita":"vita", "vita":"vita",
  "xbox":"xbox",
  "xbox 360":"xbox360",
  "xbox one":"xboxone",
  "xbox series x|s":"xboxsx", "xbox series x":"xboxsx", "xbox series s":"xboxsx", "xbox series":"xboxsx",
  "sega genesis":"genesis", "genesis":"genesis", "sega mega drive":"genesis", "mega drive":"genesis",
  "sega dreamcast":"dreamcast", "dreamcast":"dreamcast",
  "sega saturn":"saturn", "saturn":"saturn",
};
const spineStyle = (p) =>
  SPINE_LOGOS[PLAT_KEY[(p || "").trim().toLowerCase()]] ||
  { base:"#26262d", band:"#3a3a45", bandInk:"rgba(255,255,255,.82)", ink:"#f2f2f5",
    label:(p || "Unknown").toUpperCase() };

// A starting title size that lands close, so nothing paints hugely-clipped: the title
// runs down the spine, so it's the case HEIGHT the text has to fit in. shFitName then
// trims the last pixel of overflow once the element is measured for real.
function shTitlePx(t, hmm) {
  const avail = shPx(hmm) * 0.6;                 // the track left for the title after the band
  const px = avail / (Math.max((t || "").length, 1) * 0.52);  // ~0.52em per rotated latin glyph
  return Math.max(6, Math.min(10.5, Math.round(px * 2) / 2));
}

// The inner markup for a standard spine — shared by the flat shelf spine and the 3D
// case's left wall, so a pulled game's spine matches the one it came from.
function stdSpineHtml(g) {
  const s = spineStyle(g.p);
  const stripe = s.stripe ? `<i class="sp-stripe" style="background:${s.stripe}"></i>` : "";
  return stripe +
    `<span class="sp-band" style="background:${s.band};color:${s.bandInk};${s.mark || ""}">${escapeHtml(s.label)}</span>` +
    `<span class="sp-name" style="color:${s.ink};font-size:${shTitlePx(g.t, g.case.h)}px">${escapeHtml(g.t)}</span>`;
}

// Shrink a rendered title until it no longer overflows its spine. Titles run vertically,
// so overflow is along the block (scroll) axis. Cheap, and only ever run on spines the
// viewer is about to see (see shFitObs), so 1,800 spines cost nothing up front.
function shFitName(spine) {
  const nm = spine.querySelector(".sp-name");
  if (!nm) return;
  let fs = parseFloat(nm.style.fontSize) || parseFloat(getComputedStyle(nm).fontSize) || 9;
  let guard = 0;
  while (nm.scrollHeight > nm.clientHeight + 1 && fs > 5.5 && guard++ < 16) {
    fs -= 0.5;
    nm.style.fontSize = fs.toFixed(1) + "px";
  }
}
let shFitObs = null;
function shObserveFit(root) {
  if (shFitObs) shFitObs.disconnect();
  shFitObs = new IntersectionObserver((ents) => {
    for (const e of ents) if (e.isIntersecting) { shFitName(e.target); shFitObs.unobserve(e.target); }
  }, { rootMargin: "300px" });
  root.querySelectorAll(".sh-spine.std").forEach((el) => shFitObs.observe(el));
}

async function loadShelf() {
  if (SHELF.loaded) return;
  const r = await fetch("/api/shelf");
  if (!r.ok) return;
  const d = await r.json();
  SHELF.games = d.games || [];
  SHELF.loaded = true;
}

async function renderShelf() {
  const host = document.getElementById("shelfview");
  if (!SHELF.loaded) {
    host.innerHTML = `<div class="sh-empty">Reading the shelf…</div>`;
    await loadShelf();
  }
  const wraps = SHELF.games.filter((g) => g.src === "wrap").length;
  const plats = [...new Set(SHELF.games.map((g) => g.p))].sort();

  host.innerHTML = `
    <div class="sh-head">
      <div>
        <h2>My Collection</h2>
        <p class="sh-sub">
          <b>${SHELF.games.length.toLocaleString()}</b> games you can physically pick up —
          digital ones aren't objects and aren't here.
          <b>${wraps}</b> are showing the real scanned box.
        </p>
      </div>
      <div class="sh-tools">
        ${searchField("shelfsearch", "Find on the shelf…")}
        <select id="shelfplat" class="sel" aria-label="Platform">
          <option value="">All platforms</option>
          ${plats.map((p) => `<option${p === SHELF.plat ? " selected" : ""}>${escapeHtml(p)}</option>`).join("")}
        </select>
      </div>
    </div>
    <div class="sh-stage" id="shStage">
      <div class="sh-rows" id="shRows"></div>
    </div>
    <div class="sh-pull" id="shPull">
      <div class="sh-veil" id="shVeil"></div>
      <div class="sh-info" id="shInfo"></div>
    </div>`;

  const q = document.getElementById("shelfsearch");
  q.value = SHELF.filter;
  q.oninput = () => { SHELF.filter = q.value; paintShelfRows(); };
  document.getElementById("shelfplat").onchange = (e) => { SHELF.plat = e.target.value; paintShelfRows(); };
  document.getElementById("shVeil").onclick = shelfClose;
  window.addEventListener("resize", shOnResize);   // dedup'd — re-pack boards to new width
  paintShelfRows();
}

// The board width depends on the viewport, so re-pack when it changes. Debounced, and a
// no-op once the shelf is gone from the DOM.
let shResizeT = 0;
function shOnResize() {
  clearTimeout(shResizeT);
  shResizeT = setTimeout(() => { if (document.getElementById("shRows")) paintShelfRows(); }, 160);
}

/* ---------- the rows of spines ---------- */

function shelfVisible() {
  const q = SHELF.filter.trim().toLowerCase();
  return SHELF.games.filter((g) =>
    (!SHELF.plat || g.p === SHELF.plat) &&
    (!q || (g.t || "").toLowerCase().includes(q)));
}

// One spine on the flat shelf: a real scanned spine if we have a wrap, otherwise the
// system's standard spine with the title set automatically.
function shSpineHtml(g, i) {
  const real = g.src === "wrap" || g.src === "upload";
  const dims = `width:${shPx(g.case.d)}px;height:${shPx(g.case.h)}px`;
  const t = `title="${escapeHtml(g.t)} · ${escapeHtml(g.p)}${g.done ? " · Beaten" : ""}"`;
  // Beaten-at-a-glance: a check on the top of a finished game's spine. Absolute, so it
  // never touches the flex row (desktop) or the scroll snap (mobile).
  const done = g.done ? " done" : "";
  const badge = g.done ? `<i class="sh-check" aria-hidden="true"></i>` : "";
  if (real) {
    // The hue sits UNDER the scan, so a spine whose scan hasn't arrived yet is the
    // right colour rather than a black rectangle.
    const bg = `background:${g.hue} center/100% 100% no-repeat url(${faceUrl(g.k, "spine", g.uv)})`;
    return `<button class="sh-spine real${done}" data-i="${i}" ${t} style="${dims};${bg}">${badge}</button>`;
  }
  const s = spineStyle(g.p);
  return `<button class="sh-spine std${done}" data-i="${i}" ${t} style="${dims};background:${s.base}">${badge}${stdSpineHtml(g)}</button>`;
}

function paintShelfRows() {
  const rows = document.getElementById("shRows");
  if (!rows) return;
  shelfClose();
  const games = shelfVisible();
  if (!games.length) {
    rows.innerHTML = `<div class="sh-empty">Nothing on the shelf matches.</div>`;
    return;
  }
  // One CONTINUOUS shelf. Games flow across boards regardless of platform, and each
  // board highlights the platform SEGMENTS running through it — so a platform that
  // spans three boards is labelled on all three, and the shelf reads as one run.
  const idxOf = new Map(SHELF.games.map((g, i) => [g, i]));
  const total = {};
  for (const g of games) total[g.p] = (total[g.p] || 0) + 1;
  const started = new Set();

  // Pack each board to the shelf's real WIDTH rather than a fixed count. Fixed counts
  // let a wide board out-measure the viewport; flexbox then shrinks the runs while the
  // fixed-width spines inside can't shrink, so they spill over and platforms overlap.
  // Widths here mirror the CSS: 2px between spines, 7px between platform runs, 20px pad.
  const avail = Math.max((rows.clientWidth || 1100) - 20, 320);
  const boards = [];
  let cur = [], curW = 0, curPlat = null;
  for (const g of games) {
    const w = shPx(g.case.d) + 2;
    const gap = curPlat !== null && curPlat !== g.p ? 7 : 0;
    if (cur.length && curW + gap + w > avail) { boards.push(cur); cur = []; curW = 0; curPlat = null; }
    const gap2 = curPlat !== null && curPlat !== g.p ? 7 : 0;
    cur.push(g); curW += gap2 + w; curPlat = g.p;
  }
  if (cur.length) boards.push(cur);

  rows.innerHTML = boards.map((board, bi) => {
    // Consecutive same-platform spines form a labelled run within this board.
    const runs = [];
    for (const g of board) {
      const last = runs[runs.length - 1];
      if (!last || last.p !== g.p) runs.push({ p: g.p, games: [g] });
      else last.games.push(g);
    }
    return `<div class="sh-board" data-b="${bi}">
      <div class="sh-row">
        ${runs.map((run) => {
          const first = !started.has(run.p);
          started.add(run.p);
          const name = escapeHtml(run.p || "Unknown");
          return `<div class="sh-run">
            <div class="sh-spines">${run.games.map((g) => shSpineHtml(g, idxOf.get(g))).join("")}</div>
            <div class="sh-seg${first ? " head" : ""}" data-full="${name} · ${total[run.p]}${first ? "" : " · continued"}">
              <span>${name}</span>${first ? `<em>${total[run.p]}</em>` : ""}
            </div>
          </div>`;
        }).join("")}
      </div>
      <div class="sh-plank"></div>
    </div>`;
  }).join("");

  rows.onclick = (e) => {
    const b = e.target.closest(".sh-spine");
    if (b) shelfOpen(+b.dataset.i);
  };
  shObserveFit(rows);
}

/* ---------- the one case ---------- */

function shSpineAt(i) {
  const s = document.querySelector(`.sh-spine[data-i="${i}"]`);
  if (!s) return { x: innerWidth / 2, y: innerHeight };
  const r = s.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

const shState = { p: 0, vp: 0, rx: 0, ry: 0, vx: 0, vy: 0 };
let shEl = null, shCur = -1, shTarget = 0, shPending = -1, shRaf = 0, shDrag = null;
let shW = 0, shH = 0, shFrom = { x: 0, y: 0 }, shTo = { x: 0, y: 0 };
const shReduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function shelfCase() {
  if (shEl && shEl.isConnected) return shEl;
  shEl = document.createElement("div");
  shEl.className = "sh-case";
  shEl.tabIndex = 0;
  shEl.setAttribute("role", "img");
  shBindCase(shEl);
  document.getElementById("shPull").appendChild(shEl);
  return shEl;
}

function shBuild(i) {
  const g = SHELF.games[i];
  const el = shelfCase();
  shW = shPx(g.case.w); shH = shPx(g.case.h);
  el.style.setProperty("--w", shW + "px");
  el.style.setProperty("--h", shH + "px");
  el.style.setProperty("--d", shPx(g.case.d) + "px");
  el.style.width = shW + "px"; el.style.height = shH + "px";
  el.setAttribute("aria-label", `${g.t} — drag to turn`);

  const cover = g.cover ? IMG(g.cover, "cover_big") : "";
  el.innerHTML =
    (g.src === "wrap" || g.src === "upload") ? `
      <div class="sh-face f-front"><img src="${faceUrl(g.k, "front", g.uv)}" alt="" draggable="false"></div>
      <div class="sh-face f-back wrapped"><img src="${faceUrl(g.k, "back", g.uv)}" alt="" draggable="false"></div>
      <div class="sh-face f-left wrapped"><img src="${faceUrl(g.k, "spine", g.uv)}" alt="" draggable="false"></div>
      <div class="sh-face f-right"></div><div class="sh-face f-top"></div><div class="sh-face f-bottom"></div>`
    : g.src === "cover" ? `
      <div class="sh-face f-front"><img src="${cover}" alt="" draggable="false"></div>
      <div class="sh-face f-back"><img src="${cover}" alt="" draggable="false">
        <div class="sh-blurb"><i></i><i></i><i></i><i></i><i></i><span>${escapeHtml(g.p)}</span></div></div>
      <div class="sh-face f-left std" style="background:${spineStyle(g.p).base}">${stdSpineHtml(g)}</div>
      <div class="sh-face f-right"></div><div class="sh-face f-top"></div><div class="sh-face f-bottom"></div>`
    : `
      <div class="sh-face f-front sh-blank" style="--tint:${g.hue}">
        <b>${escapeHtml(g.t)}</b><small>${escapeHtml(g.p)}</small></div>
      <div class="sh-face f-back sh-blank" style="--tint:${g.hue}"></div>
      <div class="sh-face f-left std" style="background:${spineStyle(g.p).base}">${stdSpineHtml(g)}</div>
      <div class="sh-face f-right"></div><div class="sh-face f-top"></div><div class="sh-face f-bottom"></div>`;

  /* THE INSIDE OF THE BOX. Built into the 3D case itself, behind the front cover, so when the
     cover swings the media is genuinely in there — not a picture that appears beside the box.

     Built for EVERY game, even ones with nothing to put in it. The back face is
     backface-visibility:hidden (it has to be, or faces show through the closed box), which means
     that from the inside it simply is not there — so a case with no interior panel opened onto a
     view straight through the box to the shelf behind. The interior is the back wall. */
  /* …and it needs WALLS, not just a back panel. Every face of the case is backface-visibility:
     hidden, so from inside the box none of them exist — the back wall alone left the sides and top
     open, and you looked straight out through them to the shelf behind. That's the interior that
     kept coming up "invisible": not a missing panel, a missing ROOM. Four inner walls close it. */
  const inside = document.createElement("div");
  inside.className = "sh-inside";
  inside.innerHTML =
    `<span class="sh-wall w-l"></span><span class="sh-wall w-r"></span>
     <span class="sh-wall w-t"></span><span class="sh-wall w-b"></span>`
    + ((typeof mediaModelHtml === "function" && mediaFor(g.p)) ? mediaModelHtml(g) : "");
  el.appendChild(inside);
  if (typeof mountShells === "function") mountShells(inside);

  // The case element is REUSED between games. Leave it open and the next box you pull out is
  // already hanging open — which is exactly what happened.
  el.classList.remove("open", "slide");

  /* The lid is a LID, not a sheet of paper. A real case's cover has a few millimetres of plastic,
     and without it the thing that swings open is a decal. Give the front face its own free edge. */
  const lid = el.querySelector(".f-front");
  if (lid) {
    const edge = document.createElement("span");
    edge.className = "sh-lid-edge";
    lid.appendChild(edge);
  }

  // Where the case has to START: exactly on top of the spine it came from. At
  // rotateY(90) the left wall lands at world z = 0 IF the case is pushed back by half
  // its width — and then its projection IS the spine's rectangle, to the pixel.
  shFrom = shSpineAt(i);

  // Coming toward you through a lens MAGNIFIES the case, and the magnification pushes
  // away from the perspective origin, not from the case's own centre. Solve for the
  // centre that leaves a fixed margin above the SCALED top edge, or tall boxes clip.
  // Coming toward you through a lens MAGNIFIES the case, and the magnification pushes
  // away from the perspective origin — not from the case's own centre. Solve for the
  // centre that lands the case squarely in the middle of the view.
  const s = SHELF_PERSP / (SHELF_PERSP - SHELF_PULL_Z);
  const po = innerHeight * SHELF_PERSP_Y;       // must match .sh-pull perspective-origin
  const wantY = innerHeight * 0.46;
  shTo = { x: innerWidth / 2 - (innerWidth > 900 ? 130 : 0),   // leave room for the card
           y: po + (wantY - po) / s };

  const src =
    g.src === "upload"
      ? `<span class="sh-badge mine">Your upload</span>`
      : g.src === "wrap"
        ? `<span class="sh-badge real">Real box${g.region && g.region !== "user" ? " · " + escapeHtml(g.region) : ""}</span>`
        : g.src === "cover"
          ? `<span class="sh-badge fake">Front only · IGDB</span>`
          : `<span class="sh-badge none">No art anywhere</span>`;

  document.getElementById("shInfo").innerHTML = `
    <h3>${escapeHtml(g.t)}</h3>
    <div class="sh-plat">${escapeHtml(g.p)}${g.done ? ' · <span class="sh-done">Beaten</span>' : ""}</div>
    ${src}
    <dl>
      <dt>Case</dt><dd>${g.case.w} × ${g.case.h} × ${g.case.d} mm</dd>
      ${g.year ? `<dt>Released</dt><dd>${g.year}</dd>` : ""}
      ${g.series ? `<dt>Series</dt><dd>${escapeHtml(g.series)}</dd>` : ""}
    </dl>
    <div class="sh-acts">
      <button class="sh-btn primary" id="shDetails">Full details</button>
      ${typeof hasBoxContents === "function" && hasBoxContents(g.mk)
        ? `<button class="sh-btn" id="shOpen">Open the box</button>` : ""}
      <button class="sh-btn" id="shArt">${g.src === "upload" ? "Change art" : "Add / fix art"}</button>
      <button class="sh-btn" id="shBack">← Put it back</button>
    </div>
    <div id="shMedia"></div>`;
  // Open the box: the media comes out, and the booklet with it.
  const openBtn = document.getElementById("shOpen");
  if (openBtn) openBtn.onclick = () => {
    const host = document.getElementById("shMedia");
    const kase = shEl;
    const opening = !kase.classList.contains("open");
    // A bare cartridge never lived in a hinged case — it slid out of a sleeve. Cards and discs did,
    // so those still swing open. The box opens the way the real one did.
    kase.classList.toggle("slide", opensBy(g.p) === "slide");
    kase.classList.toggle("open", opening);
    openBtn.textContent = opening ? "Close the box" : "Open the box";
    host.innerHTML = opening ? mediaPanelHtml(g) : "";
    if (!opening) return;
    mountShells(host);
    const man = document.getElementById("mdManual");
    if (man) man.onclick = () => openManual(g);
  };
  document.getElementById("shBack").onclick = shelfClose;
  document.getElementById("shDetails").onclick = () => {
    // The app already has a detail card. Reuse it rather than inventing a second one.
    const row = (DATA.sheets.games.rows || []).find((r) => r._k === g.mk);
    if (row) openDrawer(row, "games");
  };
  document.getElementById("shArt").onclick = () =>
    openCoverEditor({ key: g.k, platform: g.p, title: g.t, hasUpload: g.src === "upload",
      caseDefault: g.case, existing: g.upload, onDone: () => reloadShelfBox(g.k) });
}

// After an upload changes, refresh just this game's data + the pulled case, without a
// full shelf reload.
async function reloadShelfBox(k) {
  const r = await fetch("/api/shelf");
  if (r.ok) SHELF.games = (await r.json()).games || SHELF.games;
  paintShelfRows();
  const g = SHELF.games.find((x) => x.k === k);
  if (g) shelfOpen(SHELF.games.indexOf(g));
}

function shPaint() {
  if (!shEl) return;
  const e = shState.p;
  const x = shLerp(shFrom.x, shTo.x, e) - shW / 2;
  const y = shLerp(shFrom.y, shTo.y, e) - shH / 2;
  const z = shLerp(-shW / 2, SHELF_PULL_Z, e);
  const yaw = shLerp(90, SHELF_REST_Y, e) + shState.ry * e;   // 90deg = edge-on = a spine
  shEl.style.transform =
    `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, ${z.toFixed(1)}px) ` +
    `rotateX(${(shState.rx * e).toFixed(2)}deg) rotateY(${yaw.toFixed(2)}deg)`;
  const f = shEl.querySelector(".f-front");
  if (f) f.style.setProperty("--sheen",
    (0.18 + Math.min(0.45, Math.abs(yaw - SHELF_REST_Y) / 160)).toFixed(3));
}

function shTick() {
  shRaf = 0;
  if (!shDrag) {
    // Putting a box back is snappier than pulling it out — pulling is a reveal you
    // want to watch, putting back is done. Stiffer spring on the way home.
    const home = shTarget === 0;
    shState.vp = (shState.vp + (shTarget - shState.p) * (home ? 0.26 : 0.14)) * (home ? 0.70 : 0.74);
    shState.p = shClamp(shState.p + shState.vp, -0.02, 1.06);
    if (home) {                                 // going home: unwind the turn too
      shState.vx = (shState.vx + (0 - shState.rx) * 0.20) * 0.70;
      shState.vy = (shState.vy + (0 - shState.ry) * 0.20) * 0.70;
    } else {
      shState.vx *= 0.93; shState.vy *= 0.93;   // in hand: momentum, bleeding off
    }
    shState.rx = shClamp(shState.rx + shState.vx, -SHELF_TILT_MAX, SHELF_TILT_MAX);
    shState.ry += shState.vy;
  }
  shPaint();

  const still = Math.abs(shState.vp) < 0.002 && Math.abs(shTarget - shState.p) < 0.003
    && Math.abs(shState.vx) < 0.03 && Math.abs(shState.vy) < 0.03
    && (shTarget === 1 || (Math.abs(shState.rx) < 0.1 && Math.abs(shState.ry) < 0.1));
  if (still && !shDrag) {
    shState.p = shTarget; shState.vp = 0;
    if (shTarget === 0) {                       // fully back in the row
      shState.rx = shState.ry = shState.vx = shState.vy = 0;
      shEl?.classList.remove("live");
      const s = document.querySelector(`.sh-spine[data-i="${shCur}"]`);
      s?.classList.remove("out");
      s?.nextElementSibling?.classList.remove("lean");
      document.getElementById("shPull")?.classList.remove("open");
      shCur = -1;
      syncScrollLock?.();
      if (shPending >= 0) { const n = shPending; shPending = -1; shelfOpen(n); }
      return;
    }
    shPaint(); return;
  }
  shRaf = requestAnimationFrame(shTick);
}
const shKick = () => { if (!shRaf) shRaf = requestAnimationFrame(shTick); };

function shelfOpen(i) {
  if (shCur === i) return;
  if (shCur >= 0) { shPending = i; shelfClose(); return; }   // put the current one back first
  shCur = i;
  shBuild(i);
  shState.p = 0; shState.vp = 0; shState.rx = shState.ry = shState.vx = shState.vy = 0;
  shEl.classList.add("live");
  shPaint();                                    // land on the spine BEFORE animating
  const s = document.querySelector(`.sh-spine[data-i="${i}"]`);
  s.classList.add("out");
  s.nextElementSibling?.classList.add("lean");  // its neighbour tips into the gap
  document.getElementById("shPull").classList.add("open");
  syncScrollLock?.();                            // freeze the shelf behind the 3D box
  shTarget = 1;
  if (shReduced()) { shState.p = 1; shPaint(); return; }
  shKick();
  shEl.focus({ preventScroll: true });
}

function shelfClose() {
  if (shCur < 0) return;
  shFrom = shSpineAt(shCur);      // it goes back where the spine IS now, not where it was
  shTarget = 0;
  if (shReduced()) {
    shState.p = 0; shState.rx = shState.ry = 0; shPaint();
    shEl?.classList.remove("live");
    const s = document.querySelector(`.sh-spine[data-i="${shCur}"]`);
    s?.classList.remove("out");
    s?.nextElementSibling?.classList.remove("lean");
    document.getElementById("shPull")?.classList.remove("open");
    shCur = -1;
    syncScrollLock?.();
    if (shPending >= 0) { const n = shPending; shPending = -1; shelfOpen(n); }
    return;
  }
  shKick();
}

/* ---------- turning it, once it's out ---------- */

function shBindCase(el) {
  el.addEventListener("pointerdown", (e) => {
    if (shState.p < 0.25) return;               // still coming out; let it arrive
    // Set the drag state BEFORE capturing. On iOS setPointerCapture can throw for a
    // touch pointer on a thin, edge-on element; if it did (and it wasn't guarded), the
    // drag never started and the box wouldn't turn at all on iPhone.
    shDrag = { px: e.clientX, py: e.clientY, t: e.timeStamp };
    shState.vx = shState.vy = 0;
    try { el.setPointerCapture(e.pointerId); } catch {}
    e.preventDefault();                         // no text selection, no native image drag
  }, { passive: false });
  el.addEventListener("pointermove", (e) => {
    if (!shDrag) return;
    const dt = Math.max(8, e.timeStamp - shDrag.t);
    const dy = (e.clientX - shDrag.px) * 0.55;  // drag across -> yaw
    const dx = -(e.clientY - shDrag.py) * 0.40; // drag up/down -> pitch
    shState.ry += dy;
    shState.rx = shClamp(shState.rx + dx, -SHELF_TILT_MAX, SHELF_TILT_MAX);
    shState.vy = shClamp(dy / dt * 16, -13, 13);   // deg/frame, so the glide picks up
    shState.vx = shClamp(dx / dt * 16, -13, 13);   // where your hand left off
    shDrag = { px: e.clientX, py: e.clientY, t: e.timeStamp };
    shPaint();
  });
  const release = (e) => {
    if (!shDrag) return;
    shDrag = null;
    try { el.releasePointerCapture(e.pointerId); } catch {}
    if (shReduced()) { shState.vx = shState.vy = 0; return; }
    shKick();                                   // glide to a stop, stay where it lands
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
  el.addEventListener("keydown", (e) => {
    const step = 12;
    if (e.key === "ArrowLeft") shState.ry -= step;
    else if (e.key === "ArrowRight") shState.ry += step;
    else if (e.key === "ArrowUp") shState.rx = shClamp(shState.rx + step, -SHELF_TILT_MAX, SHELF_TILT_MAX);
    else if (e.key === "ArrowDown") shState.rx = shClamp(shState.rx - step, -SHELF_TILT_MAX, SHELF_TILT_MAX);
    else return;
    e.preventDefault();
    shState.vx = shState.vy = 0;
    shPaint();
  });
}

addEventListener("keydown", (e) => {
  if (e.key === "Escape" && activeTab === "shelf" && shCur >= 0 && !document.querySelector(".drawer.open")) {
    shelfClose();
  }
});

/* ============ the cover editor (shared: shelf card + main drawer) ============
 * Two explicit choices, no auto-detect, because the user asked for it that way:
 *   "Full box art"  — a back|spine|front wrap; you drag the two guides to the spine and
 *                     we slice there. The front face takes the FRONT panel's aspect.
 *   "Front only"    — just the front; the case takes the IMAGE's aspect, so a tall Game
 *                     Boy cover isn't forced into a wide Blu-ray shape.
 * The box's proportions come from the image and from you — not a per-platform table.  */
function openCoverEditor({ key, platform, title, hasUpload, caseDefault, existing, onDone }) {
  const NOMINAL_H = (caseDefault && caseDefault.h) || 175;   // case height in mm, for size
  let kind = (existing && existing.kind) || "wrap", rotate = 0, file = null, img = null;
  // SNES/N64 art is a landscape strip with the panels lying on their side — the same
  // shape the Cover Project scans come in. Default those platforms to the per-face turn
  // (front rot90 / back rot270 / spine 0); everything else to none. Toggleable, in case
  // a given scan is already upright.
  const SIDEWAYS_PLATS = new Set(["snes", "n64"]);
  let faceRot = (existing && existing.faceRot != null)
    ? existing.faceRot
    : (SIDEWAYS_PLATS.has(PLAT_KEY[(platform || "").trim().toLowerCase()]) ? 90 : 0);
  let x1 = (existing && existing.x1) ?? 130 / 273;           // spine guides (fractions of width)
  let x2 = (existing && existing.x2) ?? 144 / 273;
  let depth = (existing && existing.d) || (caseDefault && caseDefault.d) || 14;
  // Front-only crop, as fractions of the rotated image. Front art in the wild comes with
  // a scanner bed's white margin, a shelf photo's background, or the rest of a wrap around
  // it — so the front face is whatever rectangle you drag, not the whole file. Full frame
  // by default, which is exactly the old behaviour.
  const FULL_CROP = { x1: 0, y1: 0, x2: 1, y2: 1 };
  const isFullCrop = (c) => c.x1 <= 0.001 && c.y1 <= 0.001 && c.x2 >= 0.999 && c.y2 >= 0.999;
  let crop = (existing && existing.crop) ? { ...existing.crop } : { ...FULL_CROP };

  const host = document.createElement("div");
  host.className = "ce-scrim";
  host.innerHTML = `
    <div class="ce" role="dialog" aria-label="Box art for ${escapeHtml(title)}">
      <button class="ce-x" aria-label="Close">✕</button>
      <h3>Box art</h3>
      <div class="ce-sub">${escapeHtml(title)} · ${escapeHtml(platform)}</div>

      <div class="ce-seg" role="tablist">
        <button class="ce-opt on" data-kind="wrap">Full box art</button>
        <button class="ce-opt" data-kind="front">Front only</button>
      </div>
      <p class="ce-hint" id="ceHint"></p>

      <div class="ce-drop" id="ceDrop">
        <input type="file" accept="image/*" hidden>
        <button type="button" class="ce-empty" id="ceOpen"><b>Choose an image</b><span>or drop it here</span></button>
        <div class="ce-stage" hidden>
          <div class="ce-imgwrap">
            <img alt="" draggable="false">
            <div class="ce-region back"><span>back</span></div>
            <div class="ce-region front"><span>front</span></div>
            <div class="ce-guide" data-g="1"></div>
            <div class="ce-guide" data-g="2"></div>
            <div class="ce-crop" hidden>
              <i class="ce-ch" data-h="nw"></i><i class="ce-ch" data-h="ne"></i>
              <i class="ce-ch" data-h="sw"></i><i class="ce-ch" data-h="se"></i>
            </div>
          </div>
        </div>
      </div>

      <div class="ce-tools" hidden id="ceTools">
        <button class="sh-btn" id="ceRot" type="button">↻ Rotate</button>
        <button class="sh-btn ce-side" id="ceSide" type="button"
                title="SNES/N64 scans: the panels lie on their side, so each face is turned upright">⤾ Sideways panels</button>
        <button class="sh-btn" id="ceChange" type="button">Change image</button>
        <button class="sh-btn" id="ceUncrop" type="button" hidden>⤢ Reset crop</button>
        <label class="ce-depth" id="ceDepthWrap">Spine
          <input type="range" id="ceDepth" min="4" max="40" step="1">
          <span id="ceDepthVal"></span></label>
        <span class="ce-dim" id="ceDim"></span>
      </div>

      <div class="ce-acts">
        ${hasUpload ? `<button class="sh-btn ce-rm" id="ceRemove" type="button">Remove my art</button>` : `<span></span>`}
        <div class="ce-right">
          <button class="sh-btn" id="ceCancel" type="button">Cancel</button>
          <button class="sh-btn primary" id="ceSave" type="button" disabled>Save</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(host);
  syncScrollLock?.();

  const $ = (s) => host.querySelector(s);
  const hint = $("#ceHint"), drop = $("#ceDrop"), input = drop.querySelector("input");
  const stage = $(".ce-stage"), imgwrap = $(".ce-imgwrap"), imgEl = imgwrap.querySelector("img");
  const empty = $(".ce-empty"), tools = $("#ceTools"), save = $("#ceSave"), dim = $("#ceDim");
  const sideBtn = $("#ceSide");
  const gEls = [...host.querySelectorAll(".ce-guide")];
  const regBack = $(".ce-region.back"), regFront = $(".ce-region.front");
  const depthWrap = $("#ceDepthWrap"), depthEl = $("#ceDepth"), depthVal = $("#ceDepthVal");
  const cropEl = $(".ce-crop"), uncropBtn = $("#ceUncrop");
  depthEl.value = depth;

  const HINTS = {
    wrap: "A full wrap — back, spine, front in one image. Drag the two lines onto the spine; we slice there and the front takes its own shape.",
    front: "Just the front cover. Drag the box to crop away any margin or background — the case takes the shape of what you keep, and we colour a spine and make a stand-in back.",
  };

  // The rotated image's on-screen dimensions (what the guides and slicing act on).
  const rotDims = () => rotate % 180
    ? { w: img.naturalHeight, h: img.naturalWidth }
    : { w: img.naturalWidth, h: img.naturalHeight };

  // Draw the file into a canvas at the chosen rotation, so the preview and the server
  // agree on orientation and the guides land on the real pixels.
  function paintImage() {
    if (!img) return;
    const { w, h } = rotDims();
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const ctx = c.getContext("2d");
    ctx.translate(w / 2, h / 2);
    ctx.rotate(rotate * Math.PI / 180);                       // clockwise, matches the server
    ctx.drawImage(img, -img.naturalWidth / 2, -img.naturalHeight / 2);
    imgEl.src = c.toDataURL("image/jpeg", 0.92);
    layout();
  }

  // What the front face will actually be, in image pixels — the crop, not the file.
  function cropPx() {
    const { w, h } = rotDims();
    return { w: Math.max(1, Math.round((crop.x2 - crop.x1) * w)),
             h: Math.max(1, Math.round((crop.y2 - crop.y1) * h)) };
  }

  function layout() {
    const wrap = kind === "wrap";
    gEls.forEach((g, k) => { g.hidden = !wrap; g.style.left = (k ? x2 : x1) * 100 + "%"; });
    regBack.hidden = regFront.hidden = !wrap;
    if (wrap) {
      regBack.style.width = x1 * 100 + "%";
      regFront.style.left = x2 * 100 + "%";
      regFront.style.width = (1 - x2) * 100 + "%";
    }
    // Crop is a front-only affair: in wrap mode the spine guides already say where the
    // front begins, and two overlapping rectangles would just fight each other.
    cropEl.hidden = wrap;
    uncropBtn.hidden = wrap || isFullCrop(crop);
    imgwrap.classList.toggle("cropping", !wrap);
    if (!wrap) {
      cropEl.style.left = crop.x1 * 100 + "%";
      cropEl.style.top = crop.y1 * 100 + "%";
      cropEl.style.width = (crop.x2 - crop.x1) * 100 + "%";
      cropEl.style.height = (crop.y2 - crop.y1) * 100 + "%";
    }
    imgwrap.classList.toggle("sideways", wrap && faceRot % 360 !== 0);
    sideBtn.classList.toggle("on", faceRot % 360 !== 0);
    sideBtn.hidden = !wrap;
    depthWrap.style.display = wrap ? "none" : "";             // wrap depth comes from the guides
    depthVal.textContent = depth + " mm";
    if (img) {
      const { w, h } = rotDims();
      const c = cropPx();
      dim.textContent = (!wrap && !isFullCrop(crop))
        ? `${c.w}×${c.h} · cropped from ${w}×${h}`
        : `${w}×${h}`;
    }
  }

  function setKind(k) {
    kind = k;
    host.querySelectorAll(".ce-opt").forEach((b) => b.classList.toggle("on", b.dataset.kind === k));
    hint.textContent = HINTS[k]; hint.classList.remove("ce-err");
    imgwrap.classList.toggle("is-wrap", k === "wrap");
    layout();
  }

  // Load any image blob — a freshly-chosen file, or the ORIGINAL of an existing upload
  // when reopening to adjust it. A fresh file resets rotation; a reopened one keeps it.
  function loadBlob(blob, opts = {}) {
    if (!blob || !(blob.type || "").startsWith("image/")) return;
    file = blob; rotate = opts.rotate || 0;
    crop = opts.crop ? { ...opts.crop } : { ...FULL_CROP };   // a new file starts uncropped
    const url = URL.createObjectURL(blob);
    img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      empty.hidden = true; stage.hidden = false; tools.hidden = false; save.disabled = false;
      paintImage();
    };
    img.src = url;
  }
  const loadFile = (f) => loadBlob(f, { rotate: 0 });

  // Drag a spine guide. Positions are fractions of the displayed image width.
  function dragGuide(which, clientX) {
    const r = imgwrap.getBoundingClientRect();
    let f = (clientX - r.left) / r.width;
    f = Math.max(0, Math.min(1, f));
    if (which === 0) x1 = Math.min(f, x2 - 0.01);
    else x2 = Math.max(f, x1 + 0.01);
    layout();
  }
  gEls.forEach((g, k) => {
    g.addEventListener("pointerdown", (e) => {
      e.preventDefault(); e.stopPropagation(); g.setPointerCapture(e.pointerId);
      const move = (ev) => dragGuide(k, ev.clientX);
      const up = (ev) => { g.releasePointerCapture(e.pointerId);
        g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); };
      g.addEventListener("pointermove", move); g.addEventListener("pointerup", up);
    });
  });

  // Drag the crop box: a corner resizes, anywhere else moves the whole rectangle.
  // Capture is set inside a try/catch AFTER the drag state exists — a touch pointer that
  // refuses capture must not leave the drag half-started (the iOS 3D-box bug).
  const MIN_CROP = 0.05;
  cropEl.addEventListener("pointerdown", (e) => {
    if (kind !== "front" || !img) return;
    e.preventDefault(); e.stopPropagation();
    const h = e.target.dataset.h || "";                    // "" → move, else nw/ne/sw/se
    const box = imgwrap.getBoundingClientRect();
    const from = { x: e.clientX, y: e.clientY, ...crop };
    const el = e.target;
    const move = (ev) => {
      const dx = (ev.clientX - from.x) / box.width, dy = (ev.clientY - from.y) / box.height;
      let { x1: a, y1: b, x2: c, y2: d } = from;
      if (!h) {                                            // translate, clamped to the image
        const w = c - a, ht = d - b;
        a = Math.max(0, Math.min(1 - w, a + dx)); c = a + w;
        b = Math.max(0, Math.min(1 - ht, b + dy)); d = b + ht;
      } else {
        if (h.includes("w")) a = Math.max(0, Math.min(c - MIN_CROP, a + dx));
        if (h.includes("e")) c = Math.min(1, Math.max(a + MIN_CROP, c + dx));
        if (h.includes("n")) b = Math.max(0, Math.min(d - MIN_CROP, b + dy));
        if (h.includes("s")) d = Math.min(1, Math.max(b + MIN_CROP, d + dy));
      }
      crop = { x1: a, y1: b, x2: c, y2: d };
      layout();
    };
    const up = () => {
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
  });

  host.querySelectorAll(".ce-opt").forEach((b) => b.onclick = () => setKind(b.dataset.kind));
  input.onchange = () => loadFile(input.files[0]);
  // Opening the file dialog is now an explicit button — the preview is no longer a
  // <label>, so dragging a guide can't accidentally re-trigger the picker.
  $("#ceOpen").onclick = () => input.click();
  $("#ceChange").onclick = () => input.click();
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); loadFile(e.dataTransfer.files[0]); });
  // Rotating swaps the axes, so a crop drawn against the old orientation is meaningless.
  $("#ceRot").onclick = () => { rotate = (rotate + 90) % 360; crop = { ...FULL_CROP }; paintImage(); };
  uncropBtn.onclick = () => { crop = { ...FULL_CROP }; layout(); };
  sideBtn.onclick = () => { faceRot = faceRot % 360 ? 0 : 90; layout(); };
  depthEl.oninput = () => { depth = +depthEl.value; depthVal.textContent = depth + " mm"; };
  setKind(kind);

  // Reopening a game that already has art: pull its original image back in, at the
  // rotation and guides it was saved with, so you adjust rather than start over.
  if (existing) {
    fetch(`/api/shelf/original?key=${encodeURIComponent(key)}`)
      .then((r) => (r.ok ? r.blob() : null))
      .then((b) => { if (b) loadBlob(b, { rotate: existing.rotate || 0, crop: existing.crop }); })
      .catch(() => {});
  }

  const close = () => { host.remove(); syncScrollLock?.(); };
  $("#ceCancel").onclick = close;
  $(".ce-x").onclick = close;
  host.addEventListener("click", (e) => { if (e.target === host) close(); });
  if (hasUpload) $("#ceRemove").onclick = async () => {
    $("#ceRemove").disabled = true;
    await fetch(`/api/shelf/cover?key=${encodeURIComponent(key)}`, { method: "DELETE" });
    close(); onDone && onDone();
  };

  // Compute the case dims from the image + guides, so nothing is squashed to a template.
  function caseDims() {
    const { w, h } = rotDims();
    const H = NOMINAL_H;
    // The box takes the shape of the KEPT rectangle — crop away a scanner margin and the
    // case narrows to the art, instead of the box keeping the paper's proportions.
    if (kind === "front") { const c = cropPx(); return { w: Math.round(H * (c.w / c.h)), h: H, d: depth }; }
    const frontW = (1 - x2) * w, spineW = (x2 - x1) * w;     // panel widths, in image pixels
    if (faceRot % 360) {
      // Sideways panels (SNES/N64): the front panel is frontW × h in the strip, but it
      // gets turned upright, so the real face is h × frontW — a LANDSCAPE box. The box
      // height is therefore the panel's WIDTH, and everything scales off that.
      return { w: Math.round(H * (h / frontW)), h: H,
               d: Math.max(3, Math.round(H * (spineW / frontW))) };
    }
    return { w: Math.round(H * (frontW / h)), h: H, d: Math.max(3, Math.round(H * (spineW / h))) };
  }

  save.onclick = async () => {
    if (!file) return;
    save.disabled = true; save.textContent = "Saving…";
    const c = caseDims();
    const q = new URLSearchParams({ key, kind, rotate, w: c.w, h: c.h, d: c.d });
    if (kind === "wrap") {
      q.set("x1", x1.toFixed(4)); q.set("x2", x2.toFixed(4));
      q.set("face_rot", String(faceRot % 360));
    } else if (!isFullCrop(crop)) {
      q.set("cx1", crop.x1.toFixed(4)); q.set("cy1", crop.y1.toFixed(4));
      q.set("cx2", crop.x2.toFixed(4)); q.set("cy2", crop.y2.toFixed(4));
    }
    try {
      const r = await fetch(`/api/shelf/cover?${q}`, { method: "POST", body: file });
      // HTTP/2 (production behind Traefik) sends no statusText, so reading it left the
      // message blank on every failure. Read the JSON error, then the body, then status.
      if (!r.ok) {
        let msg = "";
        try { msg = (await r.clone().json()).error; } catch { try { msg = await r.text(); } catch {} }
        throw new Error(msg || `HTTP ${r.status}`);
      }
      close(); onDone && onDone();
    } catch (err) {
      save.disabled = false; save.textContent = "Save";
      hint.textContent = "Upload failed: " + err.message; hint.classList.add("ce-err");
    }
  };
}
