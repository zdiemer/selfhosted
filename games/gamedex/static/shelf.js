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
const SHELF_PERSP_Y = 190;   // px from the board top; must match .sh-board CSS
const shClamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const shLerp = (a, b, t) => a + (b - a) * t;
const shPx = (mm) => Math.round(mm * PX_MM);
const faceUrl = (k, f) => `/api/shelf/${encodeURIComponent(k)}/${f}.jpg`;

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
      <div class="sh-veil" id="shVeil"></div>
      <div class="sh-rows" id="shRows"></div>
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

function paintShelfRows() {
  const rows = document.getElementById("shRows");
  if (!rows) return;
  shelfClose();
  const games = shelfVisible();
  if (!games.length) {
    rows.innerHTML = `<div class="sh-empty">Nothing on the shelf matches.</div>`;
    return;
  }
  // A real shelf is stacked in boards, not one endless row.
  const PER_BOARD = 44;
  const boards = [];
  for (let i = 0; i < games.length; i += PER_BOARD) boards.push(games.slice(i, i + PER_BOARD));

  rows.innerHTML = boards.map((board, bi) => `
    <div class="sh-board" data-b="${bi}">
      <div class="sh-headroom"><div class="sh-prompt">Pull a game off the shelf</div></div>
      <div class="sh-info"></div>
      <div class="sh-row">
        ${board.map((g) => {
          const i = SHELF.games.indexOf(g);
          const bg = g.src === "wrap"
            ? `background:#111 center/100% 100% url(${faceUrl(g.k, "spine")})`
            : `background:${g.hue}`;
          return `<button class="sh-spine${g.src === "wrap" ? " real" : ""}" data-i="${i}"
                     title="${escapeHtml(g.t)} · ${escapeHtml(g.p)}"
                     style="width:${shPx(g.case.d)}px;height:${shPx(g.case.h)}px;${bg}">
                    ${g.src === "wrap" ? "" : `<span>${escapeHtml(g.t)}</span>`}
                  </button>`;
        }).join("")}
      </div>
      <div class="sh-plank"></div>
    </div>`).join("");

  rows.onclick = (e) => {
    const b = e.target.closest(".sh-spine");
    if (b) shelfOpen(+b.dataset.i);
  };
}

/* ---------- the one case ---------- */

const shState = { p: 0, vp: 0, rx: 0, ry: 0, vx: 0, vy: 0 };
let shEl = null, shCur = -1, shTarget = 0, shPending = -1, shRaf = 0, shDrag = null;
let shW = 0, shH = 0, shFrom = { x: 0, y: 0 }, shTo = { x: 0, y: 0 };
const shReduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function shelfCase(board) {
  if (!shEl) {
    shEl = document.createElement("div");
    shEl.className = "sh-case";
    shEl.tabIndex = 0;
    shEl.setAttribute("role", "img");
    shBindCase(shEl);
  }
  if (shEl.parentElement !== board) board.appendChild(shEl);   // the case lives on ITS board
  return shEl;
}

function shBuild(i) {
  const g = SHELF.games[i];
  const spine = document.querySelector(`.sh-spine[data-i="${i}"]`);
  const board = spine.closest(".sh-board");
  const el = shelfCase(board);
  shW = shPx(g.case.w); shH = shPx(g.case.h);
  el.style.setProperty("--w", shW + "px");
  el.style.setProperty("--h", shH + "px");
  el.style.setProperty("--d", shPx(g.case.d) + "px");
  el.style.width = shW + "px"; el.style.height = shH + "px";
  el.setAttribute("aria-label", `${g.t} — drag to turn`);

  const cover = g.cover ? IMG(g.cover, "cover_big") : "";
  el.innerHTML =
    g.src === "wrap" ? `
      <div class="sh-face f-front"><img src="${faceUrl(g.k, "front")}" alt="" draggable="false"></div>
      <div class="sh-face f-back wrapped"><img src="${faceUrl(g.k, "back")}" alt="" draggable="false"></div>
      <div class="sh-face f-left wrapped"><img src="${faceUrl(g.k, "spine")}" alt="" draggable="false"></div>
      <div class="sh-face f-right"></div><div class="sh-face f-top"></div><div class="sh-face f-bottom"></div>`
    : g.src === "cover" ? `
      <div class="sh-face f-front"><img src="${cover}" alt="" draggable="false"></div>
      <div class="sh-face f-back"><img src="${cover}" alt="" draggable="false">
        <div class="sh-blurb"><i></i><i></i><i></i><i></i><i></i><span>${escapeHtml(g.p)}</span></div></div>
      <div class="sh-face f-left" style="background:${g.hue}"><span>${escapeHtml(g.t)}</span></div>
      <div class="sh-face f-right"></div><div class="sh-face f-top"></div><div class="sh-face f-bottom"></div>`
    : `
      <div class="sh-face f-front sh-blank" style="--tint:${g.hue}">
        <b>${escapeHtml(g.t)}</b><small>${escapeHtml(g.p)}</small></div>
      <div class="sh-face f-back sh-blank" style="--tint:${g.hue}"></div>
      <div class="sh-face f-left" style="background:${g.hue}"><span>${escapeHtml(g.t)}</span></div>
      <div class="sh-face f-right"></div><div class="sh-face f-top"></div><div class="sh-face f-bottom"></div>`;

  // Where the case has to START: exactly on top of the spine it came from. At
  // rotateY(90) the left wall lands at world z = 0 IF the case is pushed back by half
  // its width — and then its projection IS the spine's rectangle, to the pixel.
  const sr = spine.getBoundingClientRect();
  const gr = board.getBoundingClientRect();
  shFrom = { x: sr.left - gr.left + sr.width / 2, y: sr.top - gr.top + sr.height / 2 };

  // Coming toward you through a lens MAGNIFIES the case, and the magnification pushes
  // away from the perspective origin, not from the case's own centre. Solve for the
  // centre that leaves a fixed margin above the SCALED top edge, or tall boxes clip.
  const s = SHELF_PERSP / (SHELF_PERSP - SHELF_PULL_Z);
  const po = SHELF_PERSP_Y;                     // must match .sh-board perspective-origin
  shTo = { x: gr.width / 2, y: po + (SHELF_TOP_GAP - po + (shH * s) / 2) / s };

  const src = g.src === "wrap"
    ? `<span class="sh-badge real">Real box · Cover Project${g.region ? " · " + escapeHtml(g.region) : ""}</span>`
    : g.src === "cover"
      ? `<span class="sh-badge fake">Front only · IGDB</span>`
      : `<span class="sh-badge none">No art anywhere</span>`;

  board.querySelector(".sh-info").innerHTML = `
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
      <button class="sh-btn" id="shBack">← Put it back</button>
    </div>`;
  document.getElementById("shBack").onclick = shelfClose;
  document.getElementById("shDetails").onclick = () => {
    // The app already has a detail card. Reuse it rather than inventing a second one.
    const row = (DATA.sheets.games.rows || []).find((r) => r._k === g.k);
    if (row) openDrawer(row, "games");
  };
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
    shState.vp = (shState.vp + (shTarget - shState.p) * 0.14) * 0.74;
    shState.p = shClamp(shState.p + shState.vp, -0.02, 1.06);
    if (shTarget === 0) {                       // going home: unwind the turn too
      shState.vx = (shState.vx + (0 - shState.rx) * 0.09) * 0.76;
      shState.vy = (shState.vy + (0 - shState.ry) * 0.09) * 0.76;
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
      document.querySelectorAll(".sh-board.open").forEach((b) => b.classList.remove("open"));
      document.getElementById("shStage")?.classList.remove("pulled");
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
  document.querySelector(`.sh-spine[data-i="${i}"]`).closest(".sh-board").classList.add("open");
  document.getElementById("shStage").classList.add("pulled");
  shTarget = 1;
  if (shReduced()) { shState.p = 1; shPaint(); return; }
  shKick();
  shEl.focus({ preventScroll: true });
}

function shelfClose() {
  if (shCur < 0) return;
  shTarget = 0;
  if (shReduced()) {
    shState.p = 0; shState.rx = shState.ry = 0; shPaint();
    shEl?.classList.remove("live");
    const s = document.querySelector(`.sh-spine[data-i="${shCur}"]`);
    s?.classList.remove("out");
    s?.nextElementSibling?.classList.remove("lean");
    document.querySelectorAll(".sh-board.open").forEach((b) => b.classList.remove("open"));
    document.getElementById("shStage")?.classList.remove("pulled");
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
