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
const faceUrl = (k, f, v) =>
  `/api/shelf/${encodeURIComponent(k)}/${f}.jpg${v ? `?v=${v}` : ""}`;

/* A game with no scanned wrap doesn't get a flat coloured slab — it gets that
 * system's STANDARD spine: the console's brand band up top, the game's title set
 * automatically below. Brand colours are facts; the short label stands in for the
 * logo we can't reproduce. Anything not in the table falls back to a neutral spine
 * carrying the platform name, so every game reads as a real object on the shelf. */
const SPINE_LOGOS = {
  gameboy:  { base:"#9a93b4", band:"#2c2a45", bandInk:"#e8e6f2", ink:"#1b1a2a", label:"GAME BOY" },
  gbc:      { base:"#5b4fa6", band:"#2a2170", bandInk:"#ffd23f", ink:"#ffffff", label:"GBC" },
  gba:      { base:"#3a2e86", band:"#1c1550", bandInk:"#8b7bf0", ink:"#ffffff", label:"GBA" },
  ds:       { base:"#eef0f4", band:"#0a6bd6", bandInk:"#ffffff", ink:"#16181d", label:"DS" },
  n3ds:     { base:"#eef0f4", band:"#c8102e", bandInk:"#ffffff", ink:"#16181d", label:"3DS" },
  nes:      { base:"#c3c3cb", band:"#242427", bandInk:"#e6352b", ink:"#16181d", label:"NES" },
  snes:     { base:"#c9cad4", band:"#4f3f9a", bandInk:"#ffffff", ink:"#16181d", label:"SNES" },
  n64:      { base:"#1b1b21", band:"#c8102e", bandInk:"#ffffff", ink:"#f2f2f5", label:"N64" },
  gamecube: { base:"#463c85", band:"#221b4e", bandInk:"#b9b0e8", ink:"#ffffff", label:"GAMECUBE" },
  wii:      { base:"#eef1f5", band:"#0a9bd6", bandInk:"#ffffff", ink:"#16181d", label:"Wii" },
  wiiu:     { base:"#eef1f5", band:"#0a86c4", bandInk:"#ffffff", ink:"#16181d", label:"Wii U" },
  switch:   { base:"#f3f4f6", band:"#e60012", bandInk:"#ffffff", ink:"#16181d", label:"SWITCH" },
  switch2:  { base:"#f3f4f6", band:"#d1001c", bandInk:"#ffffff", ink:"#16181d", label:"SWITCH 2" },
  ps1:      { base:"#1b1b20", band:"#3a3a44", bandInk:"#e9e9ef", ink:"#e9e9ef", label:"PS one" },
  ps2:      { base:"#121219", band:"#1a3fd4", bandInk:"#ffffff", ink:"#eef0f6", label:"PS2" },
  ps3:      { base:"#0e0e13", band:"#2a2a33", bandInk:"#e9e9ef", ink:"#eef0f6", label:"PS3" },
  ps4:      { base:"#eef1f6", band:"#003791", bandInk:"#ffffff", ink:"#16181d", label:"PS4" },
  ps5:      { base:"#f5f6f9", band:"#1a1a1f", bandInk:"#ffffff", ink:"#16181d", label:"PS5" },
  psp:      { base:"#141418", band:"#2a2a33", bandInk:"#e9e9ef", ink:"#eef0f6", label:"PSP" },
  vita:     { base:"#141418", band:"#0a6bd6", bandInk:"#ffffff", ink:"#eef0f6", label:"PS VITA" },
  xbox:     { base:"#0f7d0f", band:"#0a5a0a", bandInk:"#eafff0", ink:"#ffffff", label:"XBOX" },
  xbox360:  { base:"#eef2ee", band:"#0f7d0f", bandInk:"#ffffff", ink:"#16181d", label:"XBOX 360" },
  xboxone:  { base:"#17191d", band:"#0f7d0f", bandInk:"#ffffff", ink:"#eef0f6", label:"XBOX ONE" },
  xboxsx:   { base:"#0e0f12", band:"#0f7d0f", bandInk:"#ffffff", ink:"#eef0f6", label:"SERIES X|S" },
  genesis:  { base:"#1a1a1e", band:"#c81d25", bandInk:"#ffffff", ink:"#f2f2f5", label:"GENESIS" },
  dreamcast:{ base:"#eef1f5", band:"#e35205", bandInk:"#ffffff", ink:"#16181d", label:"DREAMCAST" },
  saturn:   { base:"#141418", band:"#8a9099", bandInk:"#16181d", ink:"#eef0f6", label:"SATURN" },
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

// The inner markup for a standard spine — shared by the flat shelf spine and the 3D
// case's left wall, so a pulled game's spine matches the one it came from.
function stdSpineHtml(g) {
  const s = spineStyle(g.p);
  return `<span class="sp-band" style="background:${s.band};color:${s.bandInk}">${escapeHtml(s.label)}</span>` +
         `<span class="sp-name" style="color:${s.ink}">${escapeHtml(g.t)}</span>`;
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
  paintShelfRows();
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
  const t = `title="${escapeHtml(g.t)} · ${escapeHtml(g.p)}"`;
  if (real) {
    // The hue sits UNDER the scan, so a spine whose scan hasn't arrived yet is the
    // right colour rather than a black rectangle.
    const bg = `background:${g.hue} center/100% 100% no-repeat url(${faceUrl(g.k, "spine", g.uv)})`;
    return `<button class="sh-spine real" data-i="${i}" ${t} style="${dims};${bg}"></button>`;
  }
  const s = spineStyle(g.p);
  return `<button class="sh-spine std" data-i="${i}" ${t} style="${dims};background:${s.base}">${stdSpineHtml(g)}</button>`;
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
  const PER_BOARD = 40;
  const idxOf = new Map(SHELF.games.map((g, i) => [g, i]));
  const total = {};
  for (const g of games) total[g.p] = (total[g.p] || 0) + 1;
  const started = new Set();

  const boards = [];
  for (let i = 0; i < games.length; i += PER_BOARD) boards.push(games.slice(i, i + PER_BOARD));

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
            <div class="sh-seg${first ? " head" : ""}" title="${name}${first ? "" : " (continued)"}">
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
      <button class="sh-btn" id="shArt">${g.src === "upload" ? "Change art" : "Add / fix art"}</button>
      <button class="sh-btn" id="shBack">← Put it back</button>
    </div>`;
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
    if (shPending >= 0) { const n = shPending; shPending = -1; shelfOpen(n); }
    return;
  }
  shKick();
}

/* ---------- turning it, once it's out ---------- */

function shBindCase(el) {
  el.addEventListener("pointerdown", (e) => {
    if (shState.p < 0.25) return;               // still coming out; let it arrive
    el.setPointerCapture(e.pointerId);
    shDrag = { px: e.clientX, py: e.clientY, t: e.timeStamp };
    shState.vx = shState.vy = 0;
    e.preventDefault();                         // no text selection, no native image drag
  });
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
  let x1 = (existing && existing.x1) ?? 130 / 273;           // spine guides (fractions of width)
  let x2 = (existing && existing.x2) ?? 144 / 273;
  let depth = (existing && existing.d) || (caseDefault && caseDefault.d) || 14;

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
          </div>
        </div>
      </div>

      <div class="ce-tools" hidden id="ceTools">
        <button class="sh-btn" id="ceRot" type="button">↻ Rotate</button>
        <button class="sh-btn" id="ceChange" type="button">Change image</button>
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

  const $ = (s) => host.querySelector(s);
  const hint = $("#ceHint"), drop = $("#ceDrop"), input = drop.querySelector("input");
  const stage = $(".ce-stage"), imgwrap = $(".ce-imgwrap"), imgEl = imgwrap.querySelector("img");
  const empty = $(".ce-empty"), tools = $("#ceTools"), save = $("#ceSave"), dim = $("#ceDim");
  const gEls = [...host.querySelectorAll(".ce-guide")];
  const regBack = $(".ce-region.back"), regFront = $(".ce-region.front");
  const depthWrap = $("#ceDepthWrap"), depthEl = $("#ceDepth"), depthVal = $("#ceDepthVal");
  depthEl.value = depth;

  const HINTS = {
    wrap: "A full wrap — back, spine, front in one image. Drag the two lines onto the spine; we slice there and the front takes its own shape.",
    front: "Just the front cover. The case takes your image's exact shape, and we colour a spine and make a stand-in back.",
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
    dim.textContent = `${w}×${h}`;
    layout();
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
    depthWrap.style.display = wrap ? "none" : "";             // wrap depth comes from the guides
    depthVal.textContent = depth + " mm";
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

  host.querySelectorAll(".ce-opt").forEach((b) => b.onclick = () => setKind(b.dataset.kind));
  input.onchange = () => loadFile(input.files[0]);
  // Opening the file dialog is now an explicit button — the preview is no longer a
  // <label>, so dragging a guide can't accidentally re-trigger the picker.
  $("#ceOpen").onclick = () => input.click();
  $("#ceChange").onclick = () => input.click();
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); loadFile(e.dataTransfer.files[0]); });
  $("#ceRot").onclick = () => { rotate = (rotate + 90) % 360; paintImage(); };
  depthEl.oninput = () => { depth = +depthEl.value; depthVal.textContent = depth + " mm"; };
  setKind(kind);

  // Reopening a game that already has art: pull its original image back in, at the
  // rotation and guides it was saved with, so you adjust rather than start over.
  if (existing) {
    fetch(`/api/shelf/${encodeURIComponent(key)}/original`)
      .then((r) => (r.ok ? r.blob() : null))
      .then((b) => { if (b) loadBlob(b, { rotate: existing.rotate || 0 }); })
      .catch(() => {});
  }

  const close = () => host.remove();
  $("#ceCancel").onclick = close;
  $(".ce-x").onclick = close;
  host.addEventListener("click", (e) => { if (e.target === host) close(); });
  if (hasUpload) $("#ceRemove").onclick = async () => {
    $("#ceRemove").disabled = true;
    await fetch(`/api/shelf/${encodeURIComponent(key)}/cover`, { method: "DELETE" });
    close(); onDone && onDone();
  };

  // Compute the case dims from the image + guides, so nothing is squashed to a template.
  function caseDims() {
    const { w, h } = rotDims();
    const H = NOMINAL_H;
    if (kind === "front") return { w: Math.round(H * (w / h)), h: H, d: depth };
    const frontW = (1 - x2) * w, spineW = (x2 - x1) * w;     // in image pixels
    return { w: Math.round(H * (frontW / h)), h: H, d: Math.max(3, Math.round(H * (spineW / h))) };
  }

  save.onclick = async () => {
    if (!file) return;
    save.disabled = true; save.textContent = "Saving…";
    const c = caseDims();
    const q = new URLSearchParams({ kind, rotate, w: c.w, h: c.h, d: c.d });
    if (kind === "wrap") { q.set("x1", x1.toFixed(4)); q.set("x2", x2.toFixed(4)); }
    try {
      const r = await fetch(`/api/shelf/${encodeURIComponent(key)}/cover?${q}`,
        { method: "POST", body: file });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      close(); onDone && onDone();
    } catch (err) {
      save.disabled = false; save.textContent = "Save";
      hint.textContent = "Upload failed: " + err.message; hint.classList.add("ce-err");
    }
  };
}
