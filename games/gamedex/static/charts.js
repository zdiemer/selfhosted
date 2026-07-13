"use strict";

/* Charts for the Stats tab.

   Bars are HTML/CSS, not SVG. SVG text has no line-breaking and no ellipsis, so
   long labels ("Biggest me-vs-critic gaps" is all game titles) either overflowed
   the panel or got clipped — that was the old implementation's main visual bug.
   In HTML a label is just a <span> with text-overflow, a bar is a <button> that
   can link straight to a game, and the width animates with a plain transition.

   The donut stays SVG: arcs need paths.

   Loaded after app.js; shares its globals (openDrawer, tabState, …). */

/* Colour has exactly three jobs. Anything else is decoration, and decoration is
   what makes a chart hard to read.

   1 MAGNITUDE — one ramp, from the accent. Ordered data: a ranking, a density, a
     count. The eye reads dark-to-bright as less-to-more without being told.
   2 STATE — green/amber/red, fixed meanings, used NOWHERE else. The moment green
     means "beaten" AND "the third bar", it means nothing.
   3 CATEGORY — a few harmonised hues, only where the categories differ in KIND.

   What this replaces: twelve rotating hues handed out in render order, so a bar
   was teal because it was third. Nothing was encoded. */
const RAMP = ["#7C5CFF", "#6B4FE0", "#5B44C2", "#4C3AA3", "#3D3085", "#332A70", "#2A2360", "#241E52"];
const STATE = { good: "#35D07F", warn: "#F5A524", bad: "#F2555A" };
// Only for genuinely different KINDS of thing (platform families), never for a ranking.
const FAMILY = ["#7C5CFF", "#3DBFD9", "#4FC08D", "#E8894A", "#9AA4B8", "#C77DD6"];

// A ranked bar already encodes its value in LENGTH. The ramp reinforces that
// order instead of arguing with it.
const rampAt = (i, n) => RAMP[Math.min(RAMP.length - 1, Math.round((i / Math.max(1, n - 1)) * (RAMP.length - 1)))];
const chartColor = (i) => [FAMILY[i % FAMILY.length], FAMILY[i % FAMILY.length]];

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

/* A real tooltip, because the native SVG <title> can't do what these charts need:
   it waits a second to appear, can't be styled, and turns a list of games into an
   unreadable smear. Anything with data-tip gets one. Multi-line text is fine —
   the box renders newlines. */
let _tipEl = null;
function chartTipEl() {
  if (!_tipEl) {
    _tipEl = document.createElement("div");
    _tipEl.className = "charttip";
    _tipEl.hidden = true;
    document.body.appendChild(_tipEl);
  }
  return _tipEl;
}
const tipAttr = (text) => (text ? ` data-tip="${escapeHtml(String(text))}"` : "");

function showTip(el, ev) {
  const tip = chartTipEl();
  tip.textContent = el.getAttribute("data-tip");
  tip.hidden = false;
  const pad = 14, r = tip.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;   // flip near the right edge
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, y) + "px";
}
const hideTip = () => { if (_tipEl) _tipEl.hidden = true; };

// A bar/dot/cell knows the games behind it; say so, and cap the list.
function tipList(title, names, total) {
  const shown = names.slice(0, 6);
  const more = (total ?? names.length) - shown.length;
  return [title, ...shown.map((n) => "· " + n), more > 0 ? `+ ${more.toLocaleString()} more` : ""]
    .filter(Boolean).join("\n");
}

/* Horizontal bars — the workhorse. data: [{label, value, link?, hint?}] */
function barsH(data, opts = {}) {
  const { fmt = chartFmt, max: maxOpt, diverging = false, family = false } = opts;
  if (!data.length) return `<div class="s-empty">No data</div>`;
  const max = maxOpt || Math.max(1, ...data.map((d) => Math.abs(d.value)));
  const n = data.length;
  return `<div class="bars${diverging ? " diverging" : ""}">` + data.map((d, i) => {
    // Diverging is the ONE place two hues are earned: there's a real zero, and
    // which side of it you're on is the point. Everything else is a ranking.
    const c = diverging ? (d.value < 0 ? STATE.bad : STATE.good)
      : family ? FAMILY[i % FAMILY.length]
      : rampAt(i, n);
    const pct = Math.max(1.5, (Math.abs(d.value) / max) * 100);
    const neg = d.value < 0;
    const tag = d.link ? "button" : "div";
    // A tooltip that repeats the label and the value already printed beside it is
    // noise. Only tip when there's something MORE to say (the games behind a bar).
    return `<${tag} class="bar${d.link ? " linked" : ""}${neg ? " neg" : ""}"${chartLink(d.link)}
      ${tipAttr(d.tip)}>
      <span class="bar-lbl">${escapeHtml(String(d.label))}</span>
      <span class="bar-track">
        <span class="bar-fill" style="--w:${pct.toFixed(1)}%;--c1:${c};--d:${i * 26}ms"></span>
      </span>
      <span class="bar-val">${escapeHtml(fmt(d.value))}</span>
    </${tag}>`;
  }).join("") + `</div>`;
}

/* Vertical bars — for time series (per year, per month). */
function barsV(data, opts = {}) {
  const { fmt = chartFmt, tone = "" } = opts;
  if (!data.length) return `<div class="s-empty">No data</div>`;
  const max = Math.max(1, ...data.map((d) => d.value));
  const c1 = STATE[tone] || RAMP[0];
  // Past ~13 columns the value captions are wider than the bars they sit on and
  // start colliding. Drop them; the hover title still gives the exact figure.
  const showVals = data.length <= 13;
  return `<div class="colsw"><div class="cols${showVals ? "" : " novals"}">` + data.map((d, i) => {
    const pct = d.value ? Math.max(2, (d.value / max) * 100) : 0;
    const tag = d.link ? "button" : "div";
    // Value printed above the bar? Then the tooltip has nothing to add.
    const tip = d.tip || (showVals ? "" : `${d.label} — ${fmt(d.value)}`);
    return `<${tag} class="col${d.link ? " linked" : ""}"${chartLink(d.link)}
      ${tipAttr(tip)}>
      ${showVals ? `<span class="col-val">${d.value ? escapeHtml(fmt(d.value)) : ""}</span>` : ""}
      <span class="col-track">
        <span class="col-fill" style="--h:${pct.toFixed(1)}%;--c1:${c1};--d:${i * 22}ms"></span>
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
      fill="url(#dg${i})"/>`;
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
    // One delegated tooltip for every bar, dot, cell and spoke on the page.
    host.addEventListener("mousemove", (e) => {
      const el = e.target.closest("[data-tip]");
      if (el) showTip(el, e); else hideTip();
    });
    host.addEventListener("mouseleave", hideTip);
    host.addEventListener("scroll", hideTip, true);
    window.addEventListener("scroll", hideTip, { passive: true });
  }
  hideTip();
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

/* Area/line — a running total over time. Draws itself in with a dash offset.

   Takes one OR MORE series sharing an x axis, because a single line rarely
   answers the question on its own: "games finished" only means something next to
   "games acquired", and the gap between them IS the backlog. */
function areaLine(data, opts = {}) {
  return multiLine([{ points: data, color: opts.color || 0, label: opts.label || "" }], opts);
}

function multiLine(series, opts = {}) {
  const { fmt = chartFmt, height = 190 } = opts;
  const live = series.filter((s) => s.points && s.points.length >= 2);
  if (!live.length) return `<div class="s-empty">Not enough data</div>`;
  const W = 620, H = height, PAD = { l: 6, r: 6, t: 14, b: 22 };
  // Every series shares one x axis and one y scale — otherwise two lines on the
  // same chart would be a lie.
  //
  // The axis is the union of all series' points, SORTED. First-seen order is a
  // trap: Finished starts in 2007 and Acquired in 2008, so 2007 would land after
  // 2026 and the line would snap backwards across the chart. Points carry a
  // numeric x when they have one; otherwise fall back to arrival order.
  const xOf = new Map();
  let seq = 0;
  for (const s of live) {
    for (const p of s.points) {
      if (!xOf.has(p.label)) xOf.set(p.label, p.x != null ? p.x : seq);
      seq++;
    }
  }
  const labels = [...xOf.keys()].sort((a, b) => xOf.get(a) - xOf.get(b));
  const n = labels.length;
  const max = Math.max(1, ...live.flatMap((s) => s.points.map((p) => p.value)));
  const x = (i) => PAD.l + (n > 1 ? (i / (n - 1)) : 0) * (W - PAD.l - PAD.r);
  const y = (v) => PAD.t + (1 - v / max) * (H - PAD.t - PAD.b);

  let defs = "", paths = "";
  const at = [];      // per-series: label -> point, for the shared crosshair
  live.forEach((s, si) => {
    const [c1, c2] = chartColor(s.color ?? si);
    const at_ = new Map(s.points.map((p) => [p.label, p]));
    at.push(at_);
    const seq = labels.map((l, i) => ({ i, p: at_.get(l) })).filter((o) => o.p);
    const line = seq.map((o, k) => `${k ? "L" : "M"}${x(o.i).toFixed(1)},${y(o.p.value).toFixed(1)}`).join(" ");
    const first = seq[0], last = seq[seq.length - 1];
    const area = `${line} L${x(last.i).toFixed(1)},${(H - PAD.b).toFixed(1)} L${x(first.i).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`;
    const uid = "ln" + Math.abs(hashStr(labels.join("|") + (s.label || "") + si));
    defs += `<linearGradient id="${uid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${c1}" stop-opacity="${live.length > 1 ? ".22" : ".42"}"/>
      <stop offset="1" stop-color="${c1}" stop-opacity="0"/></linearGradient>`;
    const end = seq[seq.length - 1];
    paths += `<path class="ln-area" d="${area}" fill="url(#${uid})"/>
      <path class="ln-line" d="${line}" fill="none" stroke="${c2}" stroke-width="2.5"
        stroke-linecap="round" stroke-linejoin="round"/>
      <circle class="ln-end" cx="${x(end.i).toFixed(1)}" cy="${y(end.p.value).toFixed(1)}" r="3.6" fill="${c2}"/>`;
  });

  /* One hover target per x, spanning the full height, rather than a target per
     dot. With three lines you want to compare them AT A YEAR — hovering each
     line separately makes you hold two numbers in your head and guess at the
     third. So a band reports every series at once, and draws a guide line with a
     dot on each line it crosses. */
  const bandW = (W - PAD.l - PAD.r) / Math.max(1, n - 1);
  const bands = labels.map((l, i) => {
    const hits = live.map((s, si) => ({ s, si, p: at[si].get(l) })).filter((h) => h.p);
    if (!hits.length) return "";
    // `name` is the short series name for the tooltip. The legend `label` carries
    // a running total ("Acquired · 6,183"), which would read as nonsense here.
    const tip = [l, ...hits.map((h) => `${h.s.name ? h.s.name + ": " : ""}${fmt(h.p.value)}`)].join("\n");
    const marks = hits.map((h) => {
      const [, c2] = chartColor(h.s.color ?? h.si);
      return `<circle class="ln-mark" cx="${x(i).toFixed(1)}" cy="${y(h.p.value).toFixed(1)}" r="4" fill="${c2}"/>`;
    }).join("");
    return `<g class="ln-band"${tipAttr(tip)}>
      <rect x="${(x(i) - bandW / 2).toFixed(1)}" y="${PAD.t}" width="${bandW.toFixed(1)}"
        height="${(H - PAD.t - PAD.b).toFixed(1)}" fill="transparent"/>
      <line class="ln-guide" x1="${x(i).toFixed(1)}" y1="${PAD.t}" x2="${x(i).toFixed(1)}" y2="${(H - PAD.b).toFixed(1)}"/>
      ${marks}
    </g>`;
  }).join("");

  // Label every nth point so the axis never collides with itself.
  const step = Math.ceil(n / 10);
  const ticks = labels.map((l, i) => (i % step === 0 || i === n - 1)
    ? `<text x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="s-axis">${escapeHtml(String(l))}</text>` : "").join("");
  const legend = live.filter((s) => s.label).map((s, si) => {
    const [, c2] = chartColor(s.color ?? si);
    return `<span class="ln-key"><i style="background:${c2}"></i>${escapeHtml(s.label)}</span>`;
  }).join("");

  return `<div class="ln-wrap">
    <svg viewBox="0 0 ${W} ${H}" class="s-svg lnchart" preserveAspectRatio="xMidYMid meet">
      <defs>${defs}</defs>${paths}${bands}${ticks}
    </svg>
    ${legend ? `<div class="ln-legend">${legend}</div>` : ""}
  </div>`;
}
const hashStr = (s) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return h; };

/* Calendar heatmap — one cell per day, GitHub-style. Shows *rhythm*: the
   bar-chart-by-month version hid that you finish things in bursts. */
function heatmap(counts, year, opts = {}) {
  const { onDay = null, tipFor = null } = opts;
  const start = new Date(Date.UTC(year, 0, 1));
  const days = (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)) ? 366 : 365;
  const max = Math.max(1, ...Object.values(counts));
  const CELL = 11, GAP = 2.5, TOP = 14;
  const firstDow = start.getUTCDay();
  const weeks = Math.ceil((days + firstDow) / 7);
  const W = weeks * (CELL + GAP) + 24, H = 7 * (CELL + GAP) + TOP + 4;
  let cells = "", months = "";
  let lastMonth = -1;

  /* Pad to a full rectangle FIRST. 2026 begins on a Thursday, so Sun-Wed have no
     cell in week 0 and Fri/Sat none in the last week. The maths is right, but the
     grid reads as broken — three rows visibly short at one end, four at the other.
     These blanks are the days either side of the year: same footprint, no ink. */
  for (let wk = 0; wk < weeks; wk++) {
    for (let dow = 0; dow < 7; dow++) {
      const dayIdx = wk * 7 + dow - firstDow;
      if (dayIdx >= 0 && dayIdx < days) continue;      // a real day; drawn below
      cells += `<rect class="hm-cell hm-blank" x="${24 + wk * (CELL + GAP)}" y="${TOP + dow * (CELL + GAP)}"
        width="${CELL}" height="${CELL}" rx="2.5"/>`;
    }
  }

  for (let d = 0; d < days; d++) {
    const date = new Date(Date.UTC(year, 0, 1 + d));
    const iso = date.toISOString().slice(0, 10);
    const n = counts[iso] || 0;
    const idx = d + firstDow;
    const wk = Math.floor(idx / 7), dow = idx % 7;
    const x = 24 + wk * (CELL + GAP), y = TOP + dow * (CELL + GAP);
    const lvl = n === 0 ? 0 : Math.min(4, Math.ceil((n / max) * 4));
    const tip = n && tipFor ? tipFor(iso, n) : `${iso} — ${n} finished`;
    cells += `<rect class="hm-cell hm-l${lvl}${n && onDay ? " linked" : ""}" x="${x}" y="${y}"
      width="${CELL}" height="${CELL}" rx="2.5"${n && onDay ? chartLink(() => onDay(iso)) : ""}
      ${tipAttr(tip)} style="--d:${(wk * 6)}ms"/>`;
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
/* An INTERVAL (Gantt) chart: every item is a span on one shared timeline, packed into
   lanes so spans that overlap in time stack on top of each other. The height of the
   stack IS the answer — that is what "how many games did I have on the go at once"
   looks like.

   HTML, not SVG, for the same reason every bar here is: the labels are game titles and
   need text-overflow, which SVG text cannot do.

   Colour is the MAGNITUDE ramp keyed to DURATION — a longer playthrough is brighter, the
   same reading as a ranked bar. Identity comes from the label printed ON the bar, never
   from the hue, so nothing depends on telling two purples apart. */
function intervals(items, opts = {}) {
  if (!items.length) return `<div class="s-empty">No data</div>`;
  const LANE = 22;
  /* The domain is given, not derived. Left to itself the axis is set by the extremes,
     and one game you took three years over stretches it across those three years —
     squashing the busy stretch this chart exists to show into a sliver at the edge.
     A span that runs past the window simply gets clipped at it, which reads exactly
     right: that one was already under way. */
  const t0 = opts.from != null ? +opts.from : Math.min(...items.map((i) => +i.start));
  const t1 = opts.to != null ? +opts.to : Math.max(...items.map((i) => +i.end));
  const span = Math.max(864e5, t1 - t0);
  const pos = (t) => Math.max(0, Math.min(100, ((+t - t0) / span) * 100));

  // Greedy lane packing: drop each span into the first lane that is already free.
  const laneEnd = [];
  const placed = items.slice().sort((a, b) => +a.start - +b.start).map((it) => {
    let lane = laneEnd.findIndex((e) => e <= +it.start);
    if (lane < 0) { laneEnd.push(+it.end); lane = laneEnd.length - 1; }
    else laneEnd[lane] = +it.end;
    return { it, lane };
  });

  const maxD = Math.max(1, ...items.map((i) => +i.end - +i.start));
  const bars = placed.map(({ it, lane }) => {
    const d = +it.end - +it.start;
    const c = RAMP[Math.min(RAMP.length - 1, Math.round((1 - d / maxD) * (RAMP.length - 1)))];
    const left = pos(it.start), w = Math.max(0.7, pos(it.end) - left);
    const tag = it.link ? "button" : "div";
    return `<${tag} class="ivl-bar${it.link ? " linked" : ""}"
      style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%;top:${lane * LANE}px;background:${c}"
      ${chartLink(it.link)}${tipAttr(it.tip)}><span>${escapeHtml(String(it.label))}</span></${tag}>`;
  }).join("");

  // Gridlines at the grain the window actually needs: months for a short span, years
  // for a long one. Enough to read, not enough to shout.
  const ticks = [];
  const years = span / 3.156e10;
  if (years <= 2.5) {
    const step = years <= 1 ? 1 : 2;                     // every month, or every other
    const d = new Date(t0); d.setDate(1);
    if (+d < t0) d.setMonth(d.getMonth() + 1);
    for (; +d <= t1; d.setMonth(d.getMonth() + step)) {
      const lbl = d.getMonth() === 0
        ? String(d.getFullYear())
        : d.toLocaleDateString(undefined, { month: "short" });
      ticks.push(`<div class="ivl-tick" style="left:${pos(+d).toFixed(2)}%"><span>${lbl}</span></div>`);
    }
  } else {
    for (let y = new Date(t0).getFullYear() + 1; y <= new Date(t1).getFullYear(); y++) {
      const t = +new Date(y, 0, 1);
      if (t >= t0 && t <= t1) ticks.push(`<div class="ivl-tick" style="left:${pos(t).toFixed(2)}%"><span>${y}</span></div>`);
    }
  }

  return `<div class="ivl" style="--lanes:${laneEnd.length}">
    <div class="ivl-grid">${ticks.join("")}</div>
    <div class="ivl-bars" style="height:${laneEnd.length * LANE}px">${bars}</div>
  </div>`;
}

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
    // Above the diagonal you liked it more than the critics; below, less. That IS
    // state, so it takes the state colours — not two arbitrary hues from a rotation.
    const c1 = p.y >= p.x ? STATE.good : STATE.bad;
    const gap = Math.round((p.y - p.x) * 100);
    const tip = p.tip || [
      p.label,
      `You: ${Math.round(p.y * 100)}%`,
      `Critics: ${Math.round(p.x * 100)}%`,
      gap === 0 ? "You agreed" : `You rated it ${Math.abs(gap)} ${gap > 0 ? "higher" : "lower"}`,
    ].join("\n");
    return `<circle class="sc-dot${p.link ? " linked" : ""}" cx="${px(p.x).toFixed(1)}" cy="${py(p.y).toFixed(1)}"
      r="4.5" fill="${c1}" style="--d:${Math.min(i * 6, 700)}ms"${chartLink(p.link)}${tipAttr(tip)}/>`;
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
  /* The labels sit OUTSIDE the web and they are text, so the box has to be wider
     than it is tall — no amount of shrinking the web fixes it. "Real-Time
     Strategy" is ~95px at 10px, anchored 105px from the centre: in a square
     300-box it ran clean off the right edge. Give it horizontal gutters. */
  const GUTTER_X = 96;      // "Real-Time Strategy" is ~95px of text
  const GUTTER_Y = 24;      // the top and bottom labels need their line-height
  const W = size + GUTTER_X * 2, H = size + GUTTER_Y * 2;
  const R = size / 2 - 18, cx = W / 2, cy = H / 2;
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
    const [x, y] = pt(i, 1.14);
    const anchor = x < cx - 4 ? "end" : x > cx + 4 ? "start" : "middle";
    return `<text x="${x.toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="${anchor}" class="rd-lbl"
      ${tipAttr(a.tip || `${a.label} — ${a.hint || a.value}`)}>${escapeHtml(String(a.label))}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="s-svg radar">
    ${rings}${spokes}
    <polygon class="rd-poly" points="${poly}" fill="${c1}" fill-opacity=".28" stroke="${c2}" stroke-width="2"/>
    ${axes.map((a, i) => { const [x, y] = pt(i, a.value / max);
      return `<circle class="rd-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="7" fill="${c2}" fill-opacity=".9"
        ${tipAttr(a.tip || `${a.label} — ${a.hint || a.value}`)}/>`; }).join("")}
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
      : `<span class="poster-ph">${icon("i-library", 22)}</span>`;
    const label = note ? note(r) : "";
    return `<button class="poster"${chartLink(() => openDrawer(r, r.game ? "completed" : "games"))}
      title="${escapeHtml(String(r.title || r.game))}">
      ${art}<span class="poster-cap">${escapeHtml(String(r.title || r.game))}</span>
      ${label ? `<span class="poster-note">${escapeHtml(label)}</span>` : ""}</button>`;
  }).join("") + `</div>`;
}
