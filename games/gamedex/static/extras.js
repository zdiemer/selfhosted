"use strict";

/* Saved views · keyboard navigation · cover-derived accent colour.

   Loaded after app.js; shares its globals. */

// ---- saved views ---------------------------------------------------------
// The URL already encodes tab + search + facets + sort + view, so a "saved view"
// is just a name and a query string. No new state model needed.
const VIEWS_KEY = "gamedex.views";

const loadViews = () => {
  try { return JSON.parse(localStorage.getItem(VIEWS_KEY) || "[]"); }
  catch (_) { return []; }
};
const storeViews = (v) => prefsSave("views", v.slice(0, 24));

function describeView() {
  const st = tabState[activeTab];
  if (!st) return "";
  const bits = [];
  if (st.search) bits.push(`“${st.search}”`);
  for (const [k, set] of Object.entries(st.facets || {})) {
    if (!set || !set.size) continue;
    const col = facetColByKey(k);
    bits.push(`${col ? col.label : k}: ${[...set].slice(0, 2).join(", ")}${set.size > 2 ? "…" : ""}`);
  }
  return bits.join(" · ");
}

function renderViews() {
  const bar = $("#views");
  if (!bar) return;
  const views = loadViews();
  const st = tabState[activeTab];
  const filtered = st && (st.search || Object.keys(st.facets || {}).length);
  bar.hidden = SPECIAL_TABS.includes(activeTab) || (!views.length && !filtered);
  if (bar.hidden) return;

  bar.innerHTML =
    views.map((v, i) => `<button class="view-chip" data-vi="${i}" title="${escapeHtml(v.desc || "")}">
        ${escapeHtml(v.name)}<span class="view-x" data-vx="${i}" title="Forget this view">✕</span>
      </button>`).join("") +
    (filtered ? `<button class="view-save" id="viewSave">＋ Save this view</button>` : "");

  bar.querySelectorAll("[data-vi]").forEach((el) => {
    el.onclick = (e) => {
      if (e.target.dataset.vx !== undefined) return;    // the ✕ has its own job
      const v = loadViews()[+el.dataset.vi];
      if (!v) return;
      history.pushState({}, "", v.query || location.pathname);
      applyStateFromURL();
    };
  });
  bar.querySelectorAll("[data-vx]").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const views2 = loadViews();
      views2.splice(+el.dataset.vx, 1);
      storeViews(views2);
      renderViews();
    };
  });
  const save = $("#viewSave");
  if (save) {
    save.onclick = () => {
      const suggested = describeView().slice(0, 40) || "My view";
      const name = window.prompt("Name this view", suggested);
      if (!name) return;
      const views2 = loadViews();
      views2.unshift({ name: name.slice(0, 40), query: location.search || "", desc: describeView() });
      storeViews(views2);
      renderViews();
    };
  }
}

// ---- keyboard navigation -------------------------------------------------
// j/k (or arrows) move a selection through the grid, Enter opens it, / focuses
// search, g-then-h/g jumps tabs. Only on the browse tabs, and never while you're
// typing into something.
let kbIdx = -1;

const kbCards = () => [...document.querySelectorAll("#grid .card, #tbody tr")];
const typing = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);

function kbSelect(i) {
  const cards = kbCards();
  if (!cards.length) return;
  kbIdx = Math.max(0, Math.min(cards.length - 1, i));
  cards.forEach((c, n) => c.classList.toggle("kb", n === kbIdx));
  cards[kbIdx].scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// How many cards sit on one row of the grid — so up/down move a row, not one card.
function kbColumns() {
  const grid = $("#grid");
  if (!grid || grid.hidden) return 1;
  const cards = grid.querySelectorAll(".card");
  if (cards.length < 2) return 1;
  const top = cards[0].getBoundingClientRect().top;
  let n = 0;
  for (const c of cards) {
    if (Math.abs(c.getBoundingClientRect().top - top) > 4) break;
    n++;
  }
  return Math.max(1, n);
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (typing(document.activeElement)) return;
  if (!$("#overlay").hidden || cmdk.open) return;        // a dialog owns the keys

  if (e.key === "/") { e.preventDefault(); $("#search").focus(); $("#search").select(); return; }
  if (SPECIAL_TABS.includes(activeTab)) return;

  const cols = kbColumns();
  const step = { j: cols, k: -cols, ArrowDown: cols, ArrowUp: -cols,
                 l: 1, h: -1, ArrowRight: 1, ArrowLeft: -1 }[e.key];
  if (step !== undefined) {
    e.preventDefault();
    kbSelect(kbIdx < 0 ? 0 : kbIdx + step);
    return;
  }
  if (e.key === "Enter" && kbIdx >= 0) {
    e.preventDefault();
    const card = kbCards()[kbIdx];
    if (card) card.click();
  }
});

// A re-render replaces the cards, so drop the selection with them.
const kbReset = () => { kbIdx = -1; };

// ---- cover-derived accent ------------------------------------------------
// Sample the box art and tint the game's hero with it. IGDB serves
// `access-control-allow-origin: *`, so the canvas isn't tainted; LaunchBox and
// VNDB don't, so those throw on read and we quietly keep the default accent.
const ACCENT_CACHE = new Map();

function coverAccent(src, cb) {
  if (!src) return;
  if (ACCENT_CACHE.has(src)) return cb(ACCENT_CACHE.get(src));
  const img = new Image();
  img.crossOrigin = "anonymous";
  img.onload = () => {
    try {
      const c = document.createElement("canvas");
      const W = 24, H = 32;                       // tiny: we want an average, not detail
      c.width = W; c.height = H;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, W, H);
      const d = ctx.getImageData(0, 0, W, H).data;
      let r = 0, g = 0, b = 0, n = 0;
      for (let i = 0; i < d.length; i += 4) {
        const R = d[i], G = d[i + 1], B = d[i + 2];
        const max = Math.max(R, G, B), min = Math.min(R, G, B);
        // Skip the near-black and near-white pixels — box art is mostly letterbox
        // and logo, and averaging those gives you grey every time.
        if (max < 40 || min > 225) continue;
        const sat = max === 0 ? 0 : (max - min) / max;
        const w = 0.25 + sat;                     // let the colourful pixels win
        r += R * w; g += G * w; b += B * w; n += w;
      }
      if (!n) return;
      const accent = boostAccent(r / n, g / n, b / n);
      ACCENT_CACHE.set(src, accent);
      cb(accent);
    } catch (_) {
      /* tainted canvas (non-CORS host) — keep the default accent */
    }
  };
  img.src = src;
}

// Push the sampled colour to something usable as an accent: readable on both
// themes, and never mud.
function boostAccent(r, g, b) {
  let [h, s, l] = rgbToHsl(r, g, b);
  s = Math.min(1, Math.max(0.45, s * 1.35));
  l = Math.min(0.72, Math.max(0.55, l));
  return hslToCss(h, s, l);
}
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return [h, s, l];
}
const hslToCss = (h, s, l) => `hsl(${Math.round(h * 360)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;

// Tint the open drawer with its cover's colour.
function applyCoverAccent(row) {
  const drawer = $("#drawer");
  if (!drawer) return;
  drawer.style.removeProperty("--accent");
  drawer.style.removeProperty("--accent-line");
  drawer.style.removeProperty("--accent-soft");
  const src = coverSrc(ENRICH[row._k], "cover_big");
  coverAccent(src, (accent) => {
    drawer.style.setProperty("--accent", accent);
    drawer.style.setProperty("--accent-line", accent.replace(")", " / 45%)").replace("hsl(", "hsl("));
    drawer.style.setProperty("--accent-soft", accent.replace(")", " / 16%)").replace("hsl(", "hsl("));
  });
}

/* ---- prefs: saved views + custom challenges, server-side --------------------
   These lived in localStorage, which means they existed on exactly one browser:
   a challenge built on the desktop was invisible on the phone, and clearing site
   data threw the work away.

   The server is now the source of truth, with localStorage kept as a MIRROR —
   this is a PWA, so it has to keep working offline, and a failed write should
   never be a lost edit. Reads come from the mirror (synchronous, which is what
   every call site already assumes); writes go to both.
   -------------------------------------------------------------------------- */

// Literal, not the consts: challenges.js loads AFTER this file, so referencing
// CH_CUSTOM_KEY here would hit its temporal dead zone at parse time.
const PREFS_KEYS = { views: "gamedex.views", challenges: "gamedex.challenges" };

const prefsLocal = (key) => {
  try { return JSON.parse(localStorage.getItem(PREFS_KEYS[key]) || "[]"); }
  catch (_) { return []; }
};
const prefsMirror = (key, val) => {
  try { localStorage.setItem(PREFS_KEYS[key], JSON.stringify(val)); } catch (_) {}
};

// Write-through. The mirror updates first so the UI is never waiting on a
// round-trip, then the server catches up.
async function prefsSave(key, val) {
  prefsMirror(key, val);
  // Anonymous visitors keep views/challenges local-only — the server refuses their
  // writes (admin-only), and that refusal is by design, not an error to apologise for.
  if (typeof IS_ADMIN !== "undefined" && !IS_ADMIN) return;
  try {
    const r = await fetch(`api/prefs/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(val),
    });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) {
    // Offline, or the backend is down. The edit is safe locally and will be
    // pushed the next time anything saves; say so rather than pretend.
    if (typeof showToast === "function") showToast("Saved on this device — couldn't reach the server");
  }
}

// On boot: the server wins, except where this browser has something the server
// has never seen (the localStorage era), which gets pushed up once.
async function loadPrefs() {
  let remote = {};
  try {
    const r = await fetch("api/prefs");
    if (r.ok) remote = (await r.json()).prefs || {};
  } catch (_) {
    return;                    // offline: carry on with whatever is in the mirror
  }
  for (const key of Object.keys(PREFS_KEYS)) {
    const server = remote[key];
    const local = prefsLocal(key);
    if (Array.isArray(server) && server.length) {
      prefsMirror(key, server);                 // server is the truth
    } else if (local.length) {
      await prefsSave(key, local);              // migrate this browser's history up
      log("prefs: migrated " + local.length + " " + key + " from localStorage");
    }
  }
  if (typeof renderViews === "function") renderViews();
  if (activeTab === "challenges" && typeof renderChallenges === "function") renderChallenges();
}
const log = (m) => console.info("[gamedex] " + m);
