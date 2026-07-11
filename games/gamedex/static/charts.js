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
  if (reduce) { host.querySelectorAll(".bars, .cols, .s-donut").forEach((c) => c.classList.add("drawn")); return; }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      e.target.classList.add("drawn");
    }
  }, { threshold: 0.15 });
  host.querySelectorAll(".bars, .cols, .s-donut").forEach((c) => io.observe(c));
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
