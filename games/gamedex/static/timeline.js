"use strict";

/* Timeline — your gaming life, in order.

   1,707 completions with covers, scores and reviews, and until now the only way
   to see them was a paginated table. This is the emotional version: scroll
   through the whole thing, year by year, and watch your taste change.

   It's a VIEW MODE on the Completed tab rather than another tab — it's the same
   rows, sorted by date, and it respects whatever filters and search are active.

   Loaded after app.js; shares its globals (DATA, ENRICH, openDrawer, …). */

const TL_SNIPPET = 190;          // characters of review shown inline

function tlYearOf(r) {
  const y = String(r.date || "").slice(0, 4);
  return /^\d{4}$/.test(y) ? +y : null;
}

function tlSnippet(notes) {
  if (!notes) return "";
  const s = String(notes).replace(/\s+/g, " ").trim();
  if (s.length <= TL_SNIPPET) return s;
  // Cut on a word boundary, not mid-word.
  return s.slice(0, s.lastIndexOf(" ", TL_SNIPPET)) + "…";
}

function tlFull(notes) {
  return notes ? String(notes).replace(/\s+/g, " ").trim() : "";
}

function tlEntry(r, i) {
  const cs = coverSrc(ENRICH[r._k], "cover_big");
  const cover = cs
    ? `<img class="tl-cover tl-open" loading="lazy" src="${escapeHtml(cs)}" alt="">`
    : `<div class="tl-cover ph tl-open">${icon("i-library", 20)}</div>`;
  const score = r.rating != null
    ? `<span class="tl-score ${ratingClass(r.rating)}">${Math.round(r.rating * 100)}</span>` : "";
  const bits = [r.platform, r.playTime != null ? fmtHours(r.playTime) : null]
    .filter(Boolean).map((x) => escapeHtml(String(x))).join(" · ");
  // The review reads inline now (the old Reviews tab folded into the timeline):
  // a snippet with "Read more" to expand the full text in place. Cover + title
  // still open the drawer.
  const full = tlFull(r.notes);
  const snippet = tlSnippet(r.notes);
  const long = full.length > snippet.length;
  const review = full
    ? `<div class="tl-review">
         <p class="tl-quote">${escapeHtml(snippet)}</p>
         ${long ? `<button class="tl-more" type="button">Read more</button>` : ""}
       </div>`
    : "";
  return `<article class="tl-entry" data-tk="${escapeHtml(String(r._k || ""))}" data-ti="${i}"
      style="--d:${Math.min(i, 12) * 45}ms">
    <div class="tl-when">
      <b>${escapeHtml(r.date ? fmtDate(r.date).replace(/,? \d{4}$/, "") : "—")}</b>
    </div>
    <div class="tl-dot"></div>
    <div class="tl-card">
      ${cover}
      <div class="tl-body">
        <header><h3><button type="button" class="tl-open tl-title">${escapeHtml(String(r.game))}</button></h3>${score}</header>
        <div class="tl-meta">${bits}</div>
        ${review}
      </div>
    </div>
  </article>`;
}

// Enrichment lands after the first paint. Swap the placeholders for real covers
// in place — re-rendering 1,700 entries would flash the whole page (and it polls
// every 45s while a backfill is running).
function patchTimelineCovers() {
  const host = $("#timeline");
  if (!host || host.hidden) return;
  host.querySelectorAll(".tl-entry").forEach((el) => {
    const ph = el.querySelector(".tl-cover.ph");
    if (!ph) return;
    const cs = coverSrc(ENRICH[el.dataset.tk], "cover_big");
    if (!cs) return;
    const img = document.createElement("img");
    img.className = "tl-cover"; img.loading = "lazy"; img.alt = ""; img.src = cs;
    ph.replaceWith(img);
  });
}

// rows: already filtered by the Completed tab's search + facets.
function renderTimeline(rows) {
  const host = $("#timeline");
  const sorted = rows.slice().sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
  if (!sorted.length) {
    host.innerHTML = emptyState("Nothing to show", "No completed games match the current filters.", null);
    return;
  }

  // Group by year, newest first — a year is the unit people actually remember.
  const years = new Map();
  for (const r of sorted) {
    const y = tlYearOf(r) ?? "Undated";
    if (!years.has(y)) years.set(y, []);
    years.get(y).push(r);
  }

  let i = 0;
  const flat = [];             // index -> row, for the click handler
  const sections = [...years.entries()].map(([year, games]) => {
    const hours = games.reduce((a, g) => a + (g.playTime || 0), 0);
    const rated = games.filter((g) => g.rating != null);
    const avg = rated.length ? rated.reduce((a, g) => a + g.rating, 0) / rated.length : null;
    const entries = games.map((r) => { flat.push(r); return tlEntry(r, i++); }).join("");
    return `<section class="tl-year">
      <div class="tl-year-head">
        <h2>${escapeHtml(String(year))}</h2>
        <span class="muted">${games.length} game${games.length !== 1 ? "s" : ""}${
          hours ? ` · ${Math.round(hours).toLocaleString()}h` : ""}${
          avg != null ? ` · avg ${Math.round(avg * 100)}%` : ""}</span>
      </div>
      ${entries}
    </section>`;
  }).join("");

  host.innerHTML = `<div class="tl">${sections}</div>`;

  host.querySelectorAll(".tl-entry").forEach((el) => {
    const row = flat[+el.dataset.ti];
    // Cover and title open the drawer; the review expands in place.
    el.querySelectorAll(".tl-open").forEach((o) => o.onclick = () => { if (row) openDrawer(row, "completed"); });
    const more = el.querySelector(".tl-more");
    if (more) more.onclick = () => {
      const q = el.querySelector(".tl-quote");
      const open = el.classList.toggle("tl-expanded");
      q.textContent = open ? tlFull(row.notes) : tlSnippet(row.notes);
      more.textContent = open ? "Show less" : "Read more";
    };
  });

  // Entries fade in as they arrive, so a 1,700-item scroll doesn't just appear.
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const io = new IntersectionObserver((es) => {
      for (const e of es) {
        if (!e.isIntersecting) continue;
        io.unobserve(e.target);
        e.target.classList.add("in");
      }
    }, { threshold: 0.08 });
    host.querySelectorAll(".tl-entry").forEach((el) => io.observe(el));
  } else {
    host.querySelectorAll(".tl-entry").forEach((el) => el.classList.add("in"));
  }
}
