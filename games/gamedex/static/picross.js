"use strict";

/* The daily Picross — a nonogram cut from one of my own covers.

   The clues come from the server; the answer does not. Fill the grid, and the picture you
   uncover IS the answer — or name the game before you finish it and take the bonus.

   Rules of the interaction, which matter more than they look:
     - Drag to paint. A nonogram is dozens of cells; clicking each one is a chore. The first
       cell you touch decides the mode (fill / cross / clear) and the drag applies THAT to
       everything it crosses, so a stroke can't flip-flop under your finger.
     - Right-click (or the X tool) marks a cell as known-empty. Serious solvers live in this
       mark; without it you cannot hold a deduction in your head.
     - A clue greys out when its line is satisfied, which is the single biggest quality-of-life
       feature a nonogram can have.

   Progress is kept per day in localStorage, so a half-solved grid survives a reload. The
   streak lives in prefs (server-side), so it follows me between devices. */

const PX = {
  date: null, w: 0, h: 0, rows: [], cols: [],
  cells: [],            // 0 blank, 1 filled, 2 crossed
  solved: false, game: null, guessedEarly: false,
  drag: null,           // the value a stroke is painting
};

const pxKey = () => `picross:${PX.date}`;
const pxSave = () => {
  try {
    localStorage.setItem(pxKey(), JSON.stringify({
      cells: PX.cells, solved: PX.solved, game: PX.game, guessedEarly: PX.guessedEarly,
    }));
  } catch (_) { /* private mode: the puzzle just won't survive a reload */ }
};
const pxLoad = () => {
  try {
    const s = JSON.parse(localStorage.getItem(pxKey()) || "null");
    if (!s || !Array.isArray(s.cells)) return false;
    PX.cells = s.cells; PX.solved = !!s.solved; PX.game = s.game || null;
    PX.guessedEarly = !!s.guessedEarly;
    return true;
  } catch (_) { return false; }
};

/* ---- streak -----------------------------------------------------------------
   Kept server-side in prefs so it follows me between devices, mirrored to localStorage so
   the number is on screen instantly and survives the server being down. The app's own prefs
   helper is array-shaped (saved views, challenge history) and this is an object, so it does
   its own small load rather than bending that. */
let pxPrefs = null;
const PX_LOCAL = "gamedex.picross";
function pxStreak() {
  if (pxPrefs) return pxPrefs;
  try { pxPrefs = JSON.parse(localStorage.getItem(PX_LOCAL) || "null"); } catch (_) {}
  return (pxPrefs = pxPrefs || { streak: 0, best: 0, last: null, solved: 0 });
}
async function pxLoadPrefs() {
  try {
    const j = await (await fetch("api/prefs")).json();
    const s = (j.prefs || {}).picross;
    if (s && typeof s === "object") {
      pxPrefs = s;
      try { localStorage.setItem(PX_LOCAL, JSON.stringify(s)); } catch (_) {}
    }
  } catch (_) { /* offline: the local mirror stands in */ }
}

async function pxBumpStreak() {
  const s = { ...pxStreak() };
  if (s.last === PX.date) return s;                 // already counted today
  const y = new Date(PX.date + "T00:00:00Z");
  y.setUTCDate(y.getUTCDate() - 1);
  const yesterday = y.toISOString().slice(0, 10);
  s.streak = s.last === yesterday ? (s.streak || 0) + 1 : 1;
  s.best = Math.max(s.best || 0, s.streak);
  s.solved = (s.solved || 0) + 1;
  s.last = PX.date;
  pxPrefs = s;
  try { localStorage.setItem(PX_LOCAL, JSON.stringify(s)); } catch (_) {}
  try {
    await fetch("api/prefs/picross", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s),
    });
  } catch (_) { /* the streak is a nicety, never a blocker */ }
  return s;
}

// ---- clue helpers ----------------------------------------------------------
// A line is "done" when the filled runs it actually has match the clue it was given. Used
// only to grey the clue out — it is a convenience, not a check: the server holds the answer.
function pxRuns(line) {
  const out = []; let n = 0;
  for (const c of line) {
    if (c === 1) n++;
    else if (n) { out.push(n); n = 0; }
  }
  if (n) out.push(n);
  return out.length ? out : [0];
}
const pxLine = (y) => PX.cells.slice(y * PX.w, y * PX.w + PX.w);
const pxCol = (x) => {
  const out = [];
  for (let y = 0; y < PX.h; y++) out.push(PX.cells[y * PX.w + x]);
  return out;
};
const pxLineDone = (line, want) => {
  const got = pxRuns(line);
  return got.length === want.length && got.every((v, i) => v === want[i]);
};
const pxRowDone = (y) => pxLineDone(pxLine(y), PX.rows[y]);
const pxColDone = (x) => pxLineDone(pxCol(x), PX.cols[x]);

/* Which INDIVIDUAL clue numbers are settled — so "1 2 4" can grey the 2 and the 4 while the
   1 is still open.

   The care here is not to hand over information I haven't earned. A run only counts as
   settled if it is pinned against a boundary I have already established: work in from each
   END of the line, over cells I have decided (filled or crossed), and stop dead at the first
   cell I haven't. So a run greys out only when its position is a consequence of marks I made
   myself — never because the app peeked at the answer. Cells I have left blank, or flagged as
   a maybe, are unknown and end the scan.

   Consequence worth knowing: an interior run I've clearly identified but not yet fenced in
   with crosses stays black. That is correct. Greying it would be telling me it's in the right
   place, which is the deduction I'm supposed to be making. */
function pxSettled(line, clue) {
  const n = clue.length;
  const out = new Array(n).fill(false);
  if (clue[0] === 0) return out;
  const known = (c) => c === 1 || c === 2;          // 0 = untouched, 3 = maybe → unknown

  // from the left
  let i = 0, ci = 0;
  while (i < line.length && known(line[i]) && ci < n) {
    if (line[i] === 2) { i++; continue; }
    let run = 0;
    while (i < line.length && line[i] === 1) { run++; i++; }
    // The run has to be closed by a cell I've decided (a cross) or by the wall — otherwise
    // it might yet grow, and I don't know that it's this clue.
    const closed = i === line.length || line[i] === 2;
    if (!closed || run !== clue[ci]) break;
    out[ci++] = true;
  }
  // and from the right, meeting in the middle
  let j = line.length - 1, cj = n - 1;
  while (j >= 0 && known(line[j]) && cj >= ci) {
    if (line[j] === 2) { j--; continue; }
    let run = 0;
    while (j >= 0 && line[j] === 1) { run++; j--; }
    const closed = j < 0 || line[j] === 2;
    if (!closed || run !== clue[cj]) break;
    out[cj--] = true;
  }
  return out;
}

// ---- render ----------------------------------------------------------------
function renderPicross() {
  const host = $("#picross");
  if (!host) return;
  if (!PX.date) {
    host.innerHTML = `<div class="px-wrap"><p class="muted">Loading today's puzzle…</p></div>`;
    loadPicross();
    return;
  }
  if (PX.w === 0) {
    host.innerHTML = `<div class="px-wrap">${emptyState("No puzzle today",
      "Couldn't cut a solvable grid out of the library — it'll try again tomorrow.")}</div>`;
    return;
  }

  const st = pxStreak();
  const maxRow = Math.max(...PX.rows.map((r) => r.length));
  const maxCol = Math.max(...PX.cols.map((c) => c.length));

  const clueHtml = (nums, settled, pad) =>
    Array(pad).fill(`<i class="pad"></i>`).concat(
      nums.map((n, i) => `<i class="${settled[i] ? "ok" : ""}">${n ? n : 0}</i>`)).join("");

  const colClues = PX.cols.map((c, x) => {
    const settled = pxSettled(pxCol(x), c);
    return `<div class="px-cc${pxColDone(x) ? " done" : ""}" data-cx="${x}">${
      clueHtml(c, settled, maxCol - c.length)}</div>`;
  }).join("");

  const grid = [];
  for (let y = 0; y < PX.h; y++) {
    if (!PX.solved) {
      const r = PX.rows[y];
      const settled = pxSettled(pxLine(y), r);
      grid.push(`<div class="px-rc${pxRowDone(y) ? " done" : ""}" data-cy="${y}">${
        clueHtml(r, settled, maxRow - r.length)}</div>`);
    }
    for (let x = 0; x < PX.w; x++) {
      grid.push(`<button class="${pxCellClass(x, y)}" data-x="${x}" data-y="${y}"
        aria-label="${x + 1},${y + 1}"></button>`);
    }
  }

  host.innerHTML = `<div class="px-wrap">
    <div class="px-head">
      <div>
        <span class="h-eyebrow">${icon("i-star", 13)} Daily Picross · ${escapeHtml(PX.date)}</span>
        <h1>Draw the box art</h1>
        <p class="muted">The picture is a cover from your own shelf. Solve it to find out which —
          or name it early for the bonus.</p>
      </div>
      <div class="px-streak">
        <b>${st.streak || 0}</b><span>day streak</span>
        <em>best ${st.best || 0} · ${st.solved || 0} solved</em>
      </div>
    </div>

    ${PX.solved ? pxWinHtml() : `<div class="px-guess">
      <input id="pxGuess" type="text" list="pxTitles" placeholder="Know it already? Name the game…" autocomplete="off">
      <button class="btn" id="pxGuessGo">Guess</button>
      <span class="px-guess-msg" id="pxMsg"></span>
    </div>`}

    <!-- ONE grid: the corner, the column clues, then a row-clue gutter and its cells per row.
         The clue strips were separate grids and drifted out of alignment with the cells they
         label — the column numbers sat over the row-clue gutter. Same grid, same track sizes,
         alignment can't drift.
         Once it's solved the clues aren't rendered AT ALL: it isn't a puzzle any more, it's a
         picture, and leaving the numbers behind meant hovering the finished cover still lit up
         a row and a column of a grid that no longer exists. -->
    <div class="px-board${PX.solved ? " solved" : ""}" style="--w:${PX.w};--h:${PX.h}">
      ${PX.solved ? "" : `<div class="px-corner"></div>${colClues}`}
      ${grid.join("")}
    </div>

    <div class="px-tools">
      ${PX.solved ? "" : `
        <div class="px-tool-row">
          <button class="px-tool${pxTool === 1 ? " on" : ""}" data-tool="1">■ Fill</button>
          <button class="px-tool${pxTool === 2 ? " on" : ""}" data-tool="2">✕ Empty</button>
          <button class="px-tool${pxTool === 3 ? " on" : ""}" data-tool="3">• Maybe</button>
          <button class="px-tool ghost" id="pxClear">Clear</button>
        </div>
        <!-- Under the buttons, not beside them: a sentence of instructions wedged into the end
             of a row of controls reads as a fourth, very wide, broken button. -->
        <p class="px-hint">Drag to paint — the stroke locks to the row or column you set off in.
          Right-click for ✕, middle-click for •.</p>`}
      <div class="px-tool-row">
        <button class="px-tool ghost" id="pxReset">↺ Reset puzzle</button>
      </div>
    </div>
  </div>`;

  wirePicross(host);
  if (PX.solved) pxReveal(host);
}

// 0 untouched · 1 filled · 2 empty · 3 maybe
function pxCellClass(x, y) {
  const v = PX.cells[y * PX.w + x];
  const state = v === 1 ? " on" : v === 2 ? " x" : v === 3 ? " dot" : "";
  return "px-cell" + state
    + (x % 5 === 4 && x !== PX.w - 1 ? " vr" : "")
    + (y % 5 === 4 && y !== PX.h - 1 ? " hr" : "");
}

function pxWinHtml() {
  const g = PX.game || {};
  const cs = g.cover ? IMG(g.cover, "cover_big") : "";
  return `<div class="px-win">
    ${cs ? `<img src="${escapeHtml(cs)}" alt="">` : ""}
    <div class="px-win-b">
      <span class="h-eyebrow">${PX.guessedEarly ? "Called it" : "Solved"}</span>
      <h2>${escapeHtml(String(g.title || ""))}</h2>
      <p class="muted">${[g.platform, g.year].filter(Boolean).map((x) => escapeHtml(String(x))).join(" · ")}</p>
      ${PX.guessedEarly ? `<p class="px-bonus">★ Named it before you finished the grid.</p>` : ""}
      <button class="btn" id="pxOpen">Open in library</button>
    </div>
  </div>`;
}

let pxTool = 1;

function wirePicross(host) {
  const body = host.querySelector(".px-board");
  if (body) {
    const cellAt = (x, y) => body.querySelector(`.px-cell[data-x="${x}"][data-y="${y}"]`);

    const paint = (x, y) => {
      const i = y * PX.w + x;
      if (PX.cells[i] === PX.drag) return;
      PX.cells[i] = PX.drag;
      const el = cellAt(x, y);
      if (el) el.className = pxCellClass(x, y);
      pxRefreshClues(host);
    };

    /* Highlight the row and column under the cursor — the numbers AND the lane. On a 15x15
       grid the clue you're reading is eight cells away from the cell you're filling, and
       counting across with a fingertip is how you fill the wrong row. */
    const hover = (x, y) => {
      if (PX.solved) return;               // it's a picture now, not a puzzle
      body.querySelectorAll(".hl").forEach((el) => el.classList.remove("hl"));
      if (x == null) return;
      for (let i = 0; i < PX.w; i++) cellAt(i, y)?.classList.add("hl");
      for (let j = 0; j < PX.h; j++) cellAt(x, j)?.classList.add("hl");
      body.querySelector(`.px-rc[data-cy="${y}"]`)?.classList.add("hl");
      body.querySelector(`.px-cc[data-cx="${x}"]`)?.classList.add("hl");
    };
    body.addEventListener("pointermove", (e) => {
      const el = e.target.closest && e.target.closest(".px-cell");
      if (el) hover(+el.dataset.x, +el.dataset.y);
      else if (!PX.drag) hover(null);
    });
    body.addEventListener("pointerleave", () => { if (!PX.drag) hover(null); });

    body.addEventListener("pointerdown", (e) => {
      const el = e.target.closest(".px-cell");
      if (!el || PX.solved) return;
      e.preventDefault();
      const x0 = +el.dataset.x, y0 = +el.dataset.y;
      const i = y0 * PX.w + x0;
      // middle → maybe, right → empty, otherwise the selected tool.
      const want = e.button === 1 ? 3 : (e.button === 2 || e.ctrlKey) ? 2 : pxTool;
      // The FIRST cell decides what the whole stroke does — otherwise dragging across a
      // mixed row toggles each cell under your finger and you paint noise.
      PX.drag = PX.cells[i] === want ? 0 : want;
      /* And the first MOVEMENT decides which lane it runs in. Set off sideways and the stroke
         is that row's, for as long as the button is down — your hand can wander a cell up or
         down mid-drag (it will) and the stroke shouldn't wander with it. Undecided until you
         actually move, so a plain click still just sets one cell. */
      let axis = null;                       // null | "row" | "col"
      paint(x0, y0);
      const move = (ev) => {
        const t = document.elementFromPoint(ev.clientX, ev.clientY);
        const c = t && t.closest && t.closest(".px-cell");
        if (!c) return;
        const cx = +c.dataset.x, cy = +c.dataset.y;
        if (axis === null && (cx !== x0 || cy !== y0)) {
          axis = Math.abs(cx - x0) >= Math.abs(cy - y0) ? "row" : "col";
        }
        if (axis === "row") paint(cx, y0);
        else if (axis === "col") paint(x0, cy);
        else paint(cx, cy);
        hover(axis === "col" ? x0 : cx, axis === "row" ? y0 : cy);
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        PX.drag = null;
        pxSave();
        pxMaybeSolved();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    // Middle-click scrolls (autoscroll) on some platforms, and the context menu fights the
    // right-click mark. Neither is welcome on a puzzle board.
    body.addEventListener("contextmenu", (e) => e.preventDefault());
    body.addEventListener("auxclick", (e) => { if (e.button === 1) e.preventDefault(); });
    body.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
  }

  host.querySelectorAll("[data-tool]").forEach((el) => {
    el.onclick = () => { pxTool = +el.dataset.tool; renderPicross(); };
  });
  const clear = host.querySelector("#pxClear");
  if (clear) clear.onclick = () => {
    if (PX.solved) return;
    PX.cells = new Array(PX.w * PX.h).fill(0);
    pxSave(); renderPicross();
  };
  /* Reset goes further than Clear: it puts the day back to untouched, INCLUDING the solve,
     so you can play it again from scratch. It leaves the streak alone — you did solve it, and
     un-solving it in the UI shouldn't rewrite history. */
  const reset = host.querySelector("#pxReset");
  if (reset) reset.onclick = () => {
    if (PX.solved && !confirm("Reset today's puzzle? The board goes back to blank. Your streak is kept.")) return;
    PX.cells = new Array(PX.w * PX.h).fill(0);
    PX.solved = false; PX.game = null; PX.guessedEarly = false;
    try { localStorage.removeItem(pxKey()); } catch (_) {}
    renderPicross();
  };
  const go = host.querySelector("#pxGuessGo");
  if (go) go.onclick = pxGuess;
  const gi = host.querySelector("#pxGuess");
  if (gi) gi.onkeydown = (e) => { if (e.key === "Enter") pxGuess(); };
  const open = host.querySelector("#pxOpen");
  if (open) open.onclick = () => {
    const row = (DATA.sheets.games.rows || []).find((r) => r._k === (PX.game || {}).key);
    if (row) openDrawer(row, "games");
  };
}

// Repaint just the clue gutters — a full re-render on every painted cell would fight the drag.
function pxRefreshClues(host) {
  host.querySelectorAll(".px-rc").forEach((el) => {
    const y = +el.dataset.cy;
    el.classList.toggle("done", pxRowDone(y));
    const settled = pxSettled(pxLine(y), PX.rows[y]);
    const nums = [...el.querySelectorAll("i:not(.pad)")];
    nums.forEach((n, i) => n.classList.toggle("ok", !!settled[i]));
  });
  host.querySelectorAll(".px-cc").forEach((el) => {
    const x = +el.dataset.cx;
    el.classList.toggle("done", pxColDone(x));
    const settled = pxSettled(pxCol(x), PX.cols[x]);
    const nums = [...el.querySelectorAll("i:not(.pad)")];
    nums.forEach((n, i) => n.classList.toggle("ok", !!settled[i]));
  });
}

/* The payoff: the grid you drew becomes the box art it was cut from.

   Each cell turns into its own tile of the cover, so the picture assembles out of the
   squares you filled rather than replacing them — which is the whole point, and worth the
   fiddly bit: the tiles have to line up with the SAME crop the server cut the puzzle from
   (a square taken from the middle of the cover, biased up because box art puts the logo
   high). Get that wrong and the reveal is a picture of the game's left ear. So we wait for
   the real image, measure it, and reproduce the crop exactly. */
function pxReveal(host) {
  const g = PX.game || {};
  if (!g.cover) return;
  const board = host.querySelector(".px-board");
  const cells = [...host.querySelectorAll(".px-cell")];
  if (!board || !cells.length) return;
  const src = IMG(g.cover, "cover_big");
  const img = new Image();
  img.onload = () => {
    /* Close the grid up FIRST, then MEASURE where every cell actually landed.
       Computing the offsets from `x * (cell + gap)` looked right and was wrong: every fifth
       cell carries an extra 3px rule (the thing that lets you count to eleven), so the real
       offsets drift by 3px per block and the cover reassembled in visibly stepped bands.
       Reading each cell's true position off the layout can't drift, whatever the CSS does. */
    board.classList.add("revealed");
    void board.offsetWidth;                              // flush the gap/margin change

    const first = cells[0].getBoundingClientRect();
    const last = cells[cells.length - 1].getBoundingClientRect();
    const gridW = last.right - first.left;

    // Same crop the server cut the puzzle from (picross.py): a square of the cover's WIDTH,
    // taken 40% of the way down the leftover height — box art puts the logo high.
    const scale = gridW / img.naturalWidth;
    const cropTop = (img.naturalHeight - img.naturalWidth) * 0.40 * scale;
    const bw = gridW, bh = img.naturalHeight * scale;

    /* The picture rides on ::after, not on the cell's own background, BECAUSE
       `background-image` cannot be transitioned — setting it just swaps, which is why the
       reveal snapped in whole instead of wiping across. An overlay CAN fade, so each cell
       gets a tile that fades up on its own delay. */
    for (const el of cells) {
      const x = +el.dataset.x, y = +el.dataset.y;
      const r = el.getBoundingClientRect();
      const dx = r.left - first.left, dy = r.top - first.top;
      el.style.setProperty("--img", `url("${src}")`);
      el.style.setProperty("--bs", `${bw}px ${bh}px`);
      el.style.setProperty("--bp", `${-dx}px ${-(dy + cropTop)}px`);
      el.style.setProperty("--dly", `${(x + y) * 45}ms`);   // a diagonal wipe, not a flash
      el.classList.add("reveal");
    }
    // Two steps: lay the tiles down at opacity 0, let the browser see that, THEN light them.
    // Set both in one frame and there is no start state to transition FROM.
    requestAnimationFrame(() => requestAnimationFrame(() => board.classList.add("lit")));
  };
  img.src = src;
}

// Only ask the server when the grid COULD be right: every row and column satisfied. Saves a
// round trip on every stroke, and means a wrong answer is impossible to submit by accident.
async function pxMaybeSolved() {
  if (PX.solved) return;
  for (let y = 0; y < PX.h; y++) if (!pxRowDone(y)) return;
  for (let x = 0; x < PX.w; x++) if (!pxColDone(x)) return;
  const grid = [];
  for (let y = 0; y < PX.h; y++) {
    grid.push(Array.from({ length: PX.w }, (_, x) => (PX.cells[y * PX.w + x] === 1 ? 1 : 0)));
  }
  try {
    const r = await fetch("api/picross/solve", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ grid }),
    });
    const j = await r.json();
    if (!j.solved) return;
    PX.solved = true; PX.game = j.game;
    await pxBumpStreak();
    pxSave();
    renderPicross();
    pxCelebrate();
  } catch (_) { /* offline: the grid stays solved-looking, and it'll confirm next load */ }
}

async function pxGuess() {
  const input = $("#pxGuess"), msg = $("#pxMsg");
  const v = (input.value || "").trim();
  if (!v) return;
  try {
    const r = await fetch("api/picross/guess", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: v }),
    });
    const j = await r.json();
    if (j.correct) {
      PX.solved = true; PX.game = j.game; PX.guessedEarly = true;
      await pxBumpStreak();
      pxSave(); renderPicross(); pxCelebrate();
    } else {
      msg.textContent = "Not that one.";
      msg.className = "px-guess-msg bad";
      input.select();
    }
  } catch (_) {}
}

// A small shower of the cover's own colour. Cheap, and it makes finishing feel like finishing.
function pxCelebrate() {
  const host = $("#picross");
  if (!host || !WANTS_MOTION) return;
  const box = document.createElement("div");
  box.className = "px-confetti";
  box.innerHTML = Array.from({ length: 40 }, (_, i) =>
    `<i style="--x:${Math.random() * 100}%;--d:${(Math.random() * 0.6).toFixed(2)}s;--r:${Math.round(Math.random() * 360)}deg"></i>`).join("");
  host.appendChild(box);
  setTimeout(() => box.remove(), 2600);
}

// The guess box completes against the games it could possibly be — the ones I own or have
// finished, which is exactly the pool the puzzle is cut from. Completing against all 14,746
// would be offering answers that cannot be right.
let pxTitlesFilled = false;
function pxFillTitles() {
  if (pxTitlesFilled || !DATA || !DATA.sheets.games) return;
  const dl = $("#pxTitles");
  if (!dl) return;
  const seen = new Set();
  const opts = [];
  for (const r of DATA.sheets.games.rows) {
    if (!(r.owned || r.completed) || !r.title) continue;
    const t = String(r.title);
    if (seen.has(t)) continue;
    seen.add(t);
    opts.push(`<option value="${escapeHtml(t)}"></option>`);
  }
  dl.innerHTML = opts.join("");
  pxTitlesFilled = true;
}

/* The way in. A daily puzzle doesn't deserve a permanent seat in the nav — you play it once
   and it's done — but it does deserve to be the first thing you see on the day you haven't.
   So it lives on Home, where I land anyway, showing today's state: a thumbnail of the grid
   as far as I've got, the streak, and whether it's already done. Also in the command palette,
   and at ?tab=picross for a direct link. */
function picrossHomeCardHtml() {
  const st = pxStreak();
  const done = PX.solved;
  const started = PX.cells.some((c) => c === 1);
  const mini = PX.w
    ? `<span class="px-mini" style="--w:${PX.w}">${PX.cells.map((c) =>
        `<i class="${c === 1 ? "on" : ""}"></i>`).join("")}</span>`
    : `<span class="px-mini px-mini-ph"></span>`;
  const line = done
    ? `Solved — it was <b>${escapeHtml(String((PX.game || {}).title || "…"))}</b>`
    : started ? "Half drawn. Finish it."
    : "A cover from your shelf, hidden in a grid.";
  return `<section class="h-sect">
    <div class="h-sect-head"><h2>${icon("i-target", 17)} Daily Picross</h2></div>
    <button class="px-home${done ? " done" : ""}" id="hPicross">
      ${mini}
      <span class="px-home-b">
        <b>${done ? "Today's puzzle is done" : "Today's puzzle"}</b>
        <span class="muted">${line}</span>
      </span>
      <span class="px-home-s">
        <b>${st.streak || 0}</b><i>day streak</i>
      </span>
      <span class="gr-go">→</span>
    </button>
  </section>`;
}

function wirePicrossHome() {
  const b = document.getElementById("hPicross");
  if (b) b.onclick = () => switchTab("picross");
}

// Home needs today's state before it can draw the card, and it's a small payload.
let pxMetaLoaded = false;
async function picrossHomeInit() {
  if (pxMetaLoaded) return;
  pxMetaLoaded = true;
  await pxLoadPrefs();
  try {
    const j = await (await fetch("api/picross/daily")).json();
    if (!j.ok) return;
    PX.date = j.date; PX.w = j.w; PX.h = j.h; PX.rows = j.rows; PX.cols = j.cols;
    if (!pxLoad()) PX.cells = new Array(PX.w * PX.h).fill(0);
    if (activeTab === "home") renderHome();          // repaint with the real state
  } catch (_) { /* the card just shows an empty grid */ }
}

async function loadPicross() {
  await pxLoadPrefs();
  try {
    const r = await fetch("api/picross/daily");
    const j = await r.json();
    if (!j.ok) { PX.date = "—"; PX.w = 0; renderPicross(); return; }
    PX.date = j.date; PX.w = j.w; PX.h = j.h; PX.rows = j.rows; PX.cols = j.cols;
    if (!pxLoad()) PX.cells = new Array(PX.w * PX.h).fill(0);
    pxFillTitles();
    renderPicross();
  } catch (_) {
    PX.date = "—"; PX.w = 0; renderPicross();
  }
}
