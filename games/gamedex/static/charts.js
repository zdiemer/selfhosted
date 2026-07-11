"use strict";

/* Charts for the Stats tab.

   Bars are HTML/CSS, not SVG. SVG text has no line-breaking and no ellipsis, so
   long labels ("Biggest me-vs-critic gaps" is all game titles) either overflowed
   the panel or got clipped — that was the old implementation's main visual bug.
   In HTML a label is just a <span> with text-overflow, a bar is a <button> that
   can link straight to a game, and the width animates with a plain transition.

   The donut stays SVG: arcs need paths.

   Loaded after app.js; shares its globals (openDrawer, tabState, …). */

// Gradient pairs, one per series slot.
const CHART_COLORS = [
  ["#8b6cff", "#6d3bff"], ["#22d3ee", "#0ea5b7"], ["#34d399", "#10b981"],
  ["#fbbf24", "#f59e0b"], ["#f472b6", "#ec4899"], ["#60a5fa", "#3b82f6"],
  ["#fb923c", "#f97316"], ["#a78bfa", "#8b5cf6"], ["#2dd4bf", "#14b8a6"],
  ["#f87171", "#ef4444"], ["#e879f9", "#d946ef"], ["#facc15", "#eab308"],
];
const chartColor = (i) => CHART_COLORS[i % CHART_COLORS.length];

// Click targets are registered per render and referenced by index, so a bar can
// carry a closure (open this game / filter by this platform) without any global
// lookup table of rows.
let CHART_LINKS = [];
const resetChartLinks = () => { CHART_LINKS = []; };
function chartLink(fn) {
  if (!fn) return "";
  CHART_LINKS.push(fn);
  return ` data-cl="${CHART_LINKS.length - 1}"`;
}

const chartFmt = (v) => v.toLocaleString();

/* Horizontal bars — the workhorse. data: [{label, value, link?, hint?}] */
function barsH(data, opts = {}) {
  const { fmt = chartFmt, colorBy = "index", max: maxOpt } = opts;
  if (!data.length) return `<div class="s-empty">No data</div>`;
  // Diverging data (the me-vs-critic gaps go negative) scales on magnitude.
  const max = maxOpt || Math.max(1, ...data.map((d) => Math.abs(d.value)));
  return `<div class="bars">` + data.map((d, i) => {
    const [c1, c2] = chartColor(colorBy === "index" ? i : 0);
    const pct = Math.max(1.5, (Math.abs(d.value) / max) * 100);
    const neg = d.value < 0;
    const tag = d.link ? "button" : "div";
    return `<${tag} class="bar${d.link ? " linked" : ""}${neg ? " neg" : ""}"${chartLink(d.link)}
      title="${escapeHtml(String(d.label))} — ${escapeHtml(fmt(d.value))}">
      <span class="bar-lbl">${escapeHtml(String(d.label))}</span>
      <span class="bar-track">
        <span class="bar-fill" style="--w:${pct.toFixed(1)}%;--c1:${c1};--c2:${c2};--d:${i * 28}ms"></span>
      </span>
      <span class="bar-val">${escapeHtml(fmt(d.value))}</span>
    </${tag}>`;
  }).join("") + `</div>`;
}

/* Vertical bars — for time series (per year, per month). */
function barsV(data, opts = {}) {
  const { fmt = chartFmt, color = 0 } = opts;
  if (!data.length) return `<div class="s-empty">No data</div>`;
  const max = Math.max(1, ...data.map((d) => d.value));
  const [c1, c2] = chartColor(color);
  // Past ~13 columns the value captions are wider than the bars they sit on and
  // start colliding. Drop them; the hover title still gives the exact figure.
  const showVals = data.length <= 13;
  return `<div class="colsw"><div class="cols${showVals ? "" : " novals"}">` + data.map((d, i) => {
    const pct = d.value ? Math.max(2, (d.value / max) * 100) : 0;
    const tag = d.link ? "button" : "div";
    return `<${tag} class="col${d.link ? " linked" : ""}"${chartLink(d.link)}
      title="${escapeHtml(String(d.label))} — ${escapeHtml(fmt(d.value))}">
      ${showVals ? `<span class="col-val">${d.value ? escapeHtml(fmt(d.value)) : ""}</span>` : ""}
      <span class="col-track">
        <span class="col-fill" style="--h:${pct.toFixed(1)}%;--c1:${c1};--c2:${c2};--d:${i * 22}ms"></span>
      </span>
      <span class="col-lbl">${escapeHtml(String(d.label))}</span>
    </${tag}>`;
  }).join("") + `</div></div>`;
}

/* Donut — SVG, because arcs. */
function donut(segments, opts = {}) {
  const { size = 156 } = opts;
  const segs = segments.filter((s) => s.value);
  if (!segs.length) return `<div class="s-empty">No data</div>`;
  const total = segs.reduce((a, s) => a + s.value, 0) || 1;
  const r = size / 2, rin = r * 0.62;
  let a0 = -Math.PI / 2, paths = "", defs = "";
  segs.forEach((s, i) => {
    const [c1, c2] = chartColor(i);
    const a1 = a0 + 2 * Math.PI * (s.value / total);
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (ang, rad) => [(r + rad * Math.cos(ang)).toFixed(2), (r + rad * Math.sin(ang)).toFixed(2)];
    const [x0, y0] = p(a0, r), [x1, y1] = p(a1, r), [xi0, yi0] = p(a1, rin), [xi1, yi1] = p(a0, rin);
    defs += `<linearGradient id="dg${i}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient>`;
    paths += `<path class="donut-seg" style="--d:${i * 60}ms"
      d="M${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} L${xi0},${yi0} A${rin},${rin} 0 ${large} 0 ${xi1},${yi1} Z"
      fill="url(#dg${i})"><title>${escapeHtml(String(s.label))} — ${s.value.toLocaleString()} (${Math.round(100 * s.value / total)}%)</title></path>`;
    a0 = a1;
  });
  const legend = segs.map((s, i) => {
    const tag = s.link ? "button" : "div";
    return `<${tag} class="s-leg${s.link ? " linked" : ""}"${chartLink(s.link)}>
      <span style="background:linear-gradient(135deg,${chartColor(i)[0]},${chartColor(i)[1]})"></span>
      <span class="s-leg-lbl">${escapeHtml(String(s.label))}</span> <b>${s.value.toLocaleString()}</b></${tag}>`;
  }).join("");
  return `<div class="s-donut-wrap">
    <svg viewBox="0 0 ${size} ${size}" class="s-donut"><defs>${defs}</defs>${paths}
      <text x="${r}" y="${r}" class="donut-total">${total.toLocaleString()}</text></svg>
    <div class="s-legend">${legend}</div></div>`;
}

/* Big numbers count up from zero when the card scrolls into view. */
function animateCounters(host) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      const el = e.target;
      const target = parseFloat(el.dataset.n);
      if (!isFinite(target)) continue;
      const pre = el.dataset.pre || "", post = el.dataset.post || "";
      const dur = 900, t0 = performance.now();
      const step = (t) => {
        const p = Math.min(1, (t - t0) / dur);
        const eased = 1 - Math.pow(1 - p, 3);          // ease-out cubic
        const v = target * eased;
        el.textContent = pre + Math.round(v).toLocaleString() + post;
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
  }, { threshold: 0.4 });
  host.querySelectorAll("[data-n]").forEach((el) => io.observe(el));
}

/* Bars animate from zero once their panel is on screen. */
function animateCharts(host) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) { host.querySelectorAll(".bars, .cols, .s-svg").forEach((c) => c.classList.add("drawn")); return; }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      e.target.classList.add("drawn");
    }
  }, { threshold: 0.15 });
  host.querySelectorAll(".bars, .cols, .s-svg").forEach((c) => io.observe(c));
}

// One delegated listener for every linked bar/legend row on the page. Bound
// once — renderStats() runs on every tab switch and theme change, and the host
// element survives, so re-binding here would stack duplicate handlers.
let _chartsBound = false;
function wireCharts(host) {
  if (!_chartsBound) {
    _chartsBound = true;
    host.addEventListener("click", (e) => {
      const el = e.target.closest("[data-cl]");
      if (!el) return;
      const fn = CHART_LINKS[+el.dataset.cl];
      if (fn) fn();
    });
  }
  animateCharts(host);
  animateCounters(host);
}

// A bar that opens a game's detail drawer.
const gameLink = (row, sheet) => () => openDrawer(row, sheet);
// A bar that filters a tab by one facet value.
const facetLink = (tab, key, val) => () => {
  const st = tabState[tab];
  if (!st) return;
  st.facets = { [key]: new Set([String(val)]) };
  st.search = ""; st.page = 1;
  switchTab(tab);
  nav();
};

/* ==========================================================================
   Richer chart types. Bars and donuts everywhere made the Stats page read
   same-y; these give each question a shape that suits it.
   ========================================================================== */

/* Area/line — a running total over time. Draws itself in with a dash offset. */
function areaLine(data, opts = {}) {
  const { fmt = chartFmt, color = 0, height = 190, label = "" } = opts;
  if (data.length < 2) return `<div class="s-empty">Not enough data</div>`;
  const W = 620, H = height, PAD = { l: 6, r: 6, t: 14, b: 22 };
  const [c1, c2] = chartColor(color);
  const max = Math.max(1, ...data.map((d) => d.value));
  const x = (i) => PAD.l + (i / (data.length - 1)) * (W - PAD.l - PAD.r);
  const y = (v) => PAD.t + (1 - v / max) * (H - PAD.t - PAD.b);
  const pts = data.map((d, i) => [x(i), y(d.value)]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${x(data.length - 1).toFixed(1)},${(H - PAD.b).toFixed(1)} L${x(0).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`;
  // Label every nth point so the axis never collides with itself.
  const step = Math.ceil(data.length / 10);
  const ticks = data.map((d, i) => (i % step === 0 || i === data.length - 1)
    ? `<text x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="s-axis">${escapeHtml(String(d.label))}</text>` : "").join("");
  const dots = data.map((d, i) =>
    `<circle class="ln-dot" cx="${x(i).toFixed(1)}" cy="${y(d.value).toFixed(1)}" r="8" fill="transparent">
       <title>${escapeHtml(String(d.label))} — ${escapeHtml(fmt(d.value))}</title></circle>`).join("");
  const uid = "ln" + Math.abs(hashStr(JSON.stringify(data.map((d) => d.label)) + color));
  return `<svg viewBox="0 0 ${W} ${H}" class="s-svg lnchart" preserveAspectRatio="xMidYMid meet">
    <defs><linearGradient id="${uid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${c1}" stop-opacity=".42"/>
      <stop offset="1" stop-color="${c1}" stop-opacity="0"/></linearGradient></defs>
    <path class="ln-area" d="${area}" fill="url(#${uid})"/>
    <path class="ln-line" d="${line}" fill="none" stroke="${c2}" stroke-width="2.5"
      stroke-linecap="round" stroke-linejoin="round"/>
    ${dots}${ticks}
    ${label ? `<text x="${PAD.l}" y="12" class="s-val">${escapeHtml(label)}</text>` : ""}
  </svg>`;
}
const hashStr = (s) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return h; };

/* Calendar heatmap — one cell per day, GitHub-style. Shows *rhythm*: the
   bar-chart-by-month version hid that you finish things in bursts. */
function heatmap(counts, year, opts = {}) {
  const { onDay = null } = opts;
  const start = new Date(Date.UTC(year, 0, 1));
  const days = (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)) ? 366 : 365;
  const max = Math.max(1, ...Object.values(counts));
  const CELL = 11, GAP = 2.5, TOP = 14;
  const firstDow = start.getUTCDay();
  const weeks = Math.ceil((days + firstDow) / 7);
  const W = weeks * (CELL + GAP) + 24, H = 7 * (CELL + GAP) + TOP + 4;
  let cells = "", months = "";
  let lastMonth = -1;
  for (let d = 0; d < days; d++) {
    const date = new Date(Date.UTC(year, 0, 1 + d));
    const iso = date.toISOString().slice(0, 10);
    const n = counts[iso] || 0;
    const idx = d + firstDow;
    const wk = Math.floor(idx / 7), dow = idx % 7;
    const x = 24 + wk * (CELL + GAP), y = TOP + dow * (CELL + GAP);
    const lvl = n === 0 ? 0 : Math.min(4, Math.ceil((n / max) * 4));
    cells += `<rect class="hm-cell hm-l${lvl}${n && onDay ? " linked" : ""}" x="${x}" y="${y}"
      width="${CELL}" height="${CELL}" rx="2.5"${n && onDay ? chartLink(() => onDay(iso)) : ""}
      style="--d:${(wk * 6)}ms"><title>${iso} — ${n} finished</title></rect>`;
    const m = date.getUTCMonth();
    if (m !== lastMonth && dow <= 1) {
      months += `<text x="${x}" y="9" class="s-axis">${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m]}</text>`;
      lastMonth = m;
    }
  }
  const dowLbl = ["", "Mon", "", "Wed", "", "Fri", ""].map((l, i) =>
    l ? `<text x="0" y="${TOP + i * (CELL + GAP) + 9}" class="s-axis">${l}</text>` : "").join("");
  return `<div class="hm-wrap"><svg viewBox="0 0 ${W} ${H}" class="s-svg heatmap">${months}${dowLbl}${cells}</svg>
    <div class="hm-key"><span>Less</span>
      ${[0,1,2,3,4].map((l) => `<span class="hm-cell hm-l${l}"></span>`).join("")}
      <span>More</span></div></div>`;
}

/* Scatter — every finished game as a dot: critics across, you up. The diagonal
   is agreement; distance from it is how contrarian you were. */
function scatter(points, opts = {}) {
  const { xLabel = "Critics", yLabel = "You", size = 300 } = opts;
  if (!points.length) return `<div class="s-empty">No data</div>`;
  const W = 340, H = 300, PAD = { l: 34, r: 10, t: 14, b: 26 };
  const px = (v) => PAD.l + v * (W - PAD.l - PAD.r);
  const py = (v) => H - PAD.b - v * (H - PAD.t - PAD.b);
  const grid = [0, .25, .5, .75, 1].map((v) =>
    `<line class="sc-grid" x1="${PAD.l}" y1="${py(v).toFixed(1)}" x2="${W - PAD.r}" y2="${py(v).toFixed(1)}"/>
     <text x="${PAD.l - 6}" y="${(py(v) + 3).toFixed(1)}" text-anchor="end" class="s-axis">${Math.round(v * 100)}</text>`).join("");
  const dots = points.map((p, i) => {
    const [c1] = chartColor(p.y >= p.x ? 2 : 9);      // above the line = you liked it more
    return `<circle class="sc-dot${p.link ? " linked" : ""}" cx="${px(p.x).toFixed(1)}" cy="${py(p.y).toFixed(1)}"
      r="4.5" fill="${c1}" style="--d:${Math.min(i * 6, 700)}ms"${chartLink(p.link)}>
      <title>${escapeHtml(p.label)} — you ${Math.round(p.y * 100)}, critics ${Math.round(p.x * 100)}</title></circle>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="s-svg scatter">
    ${grid}
    <line class="sc-diag" x1="${px(0)}" y1="${py(0)}" x2="${px(1)}" y2="${py(1)}"/>
    ${dots}
    <text x="${W / 2}" y="${H - 4}" text-anchor="middle" class="s-axis">${escapeHtml(xLabel)} →</text>
    <text transform="rotate(-90 9 ${H / 2})" x="9" y="${H / 2}" text-anchor="middle" class="s-axis">${escapeHtml(yLabel)} ↑</text>
  </svg>`;
}

/* Radar — a shape for your taste. One spoke per genre. */
function radar(axes, opts = {}) {
  const { size = 260, color = 0 } = opts;
  if (axes.length < 3) return `<div class="s-empty">Not enough data</div>`;
  const R = size / 2 - 34, cx = size / 2, cy = size / 2;
  const [c1, c2] = chartColor(color);
  const max = Math.max(...axes.map((a) => a.value)) || 1;
  const pt = (i, frac) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / axes.length;
    return [cx + Math.cos(ang) * R * frac, cy + Math.sin(ang) * R * frac];
  };
  const rings = [.25, .5, .75, 1].map((f) =>
    `<polygon class="rd-ring" points="${axes.map((_, i) => pt(i, f).map((n) => n.toFixed(1)).join(",")).join(" ")}"/>`).join("");
  const spokes = axes.map((_, i) => {
    const [x, y] = pt(i, 1);
    return `<line class="rd-ring" x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
  }).join("");
  const poly = axes.map((a, i) => pt(i, a.value / max).map((n) => n.toFixed(1)).join(",")).join(" ");
  const labels = axes.map((a, i) => {
    const [x, y] = pt(i, 1.19);
    const anchor = x < cx - 4 ? "end" : x > cx + 4 ? "start" : "middle";
    return `<text x="${x.toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="${anchor}" class="rd-lbl">
      ${escapeHtml(String(a.label))}<title>${escapeHtml(String(a.label))} — ${a.hint || a.value}</title></text>`;
  }).join("");
  return `<svg viewBox="0 0 ${size} ${size}" class="s-svg radar">
    ${rings}${spokes}
    <polygon class="rd-poly" points="${poly}" fill="${c1}" fill-opacity=".28" stroke="${c2}" stroke-width="2"/>
    ${axes.map((a, i) => { const [x, y] = pt(i, a.value / max);
      return `<circle class="rd-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${c2}"/>`; }).join("")}
    ${labels}
  </svg>`;
}

/* Pictures. The page was wall-to-wall geometry; these put the games back in it. */
function posterRow(rows, opts = {}) {
  const { note = null } = opts;
  if (!rows.length) return `<div class="s-empty">No data</div>`;
  return `<div class="posters">` + rows.map((r) => {
    const cs = coverSrc(ENRICH[r._k], "cover_big");
    const art = cs
      ? `<img loading="lazy" src="${escapeHtml(cs)}" alt="">`
      : `<span class="poster-ph">🎮</span>`;
    const label = note ? note(r) : "";
    return `<button class="poster"${chartLink(() => openDrawer(r, r.game ? "completed" : "games"))}
      title="${escapeHtml(String(r.title || r.game))}">
      ${art}<span class="poster-cap">${escapeHtml(String(r.title || r.game))}</span>
      ${label ? `<span class="poster-note">${escapeHtml(label)}</span>` : ""}</button>`;
  }).join("") + `</div>`;
}
