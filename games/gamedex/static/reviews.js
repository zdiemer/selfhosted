"use strict";

/* The reviews reader.

   The completed sheet's Notes column holds ~714 long-form reviews — nearly half
   a million characters of writing. Until now they were buried at the bottom of a
   drawer, inside a collapsed "Raw data" block. This gives them a reading view.

   Loaded after app.js; shares its globals (DATA, ENRICH, openDrawer, …). */

const MIN_REVIEW = 180;            // shorter than this is a note, not a review
const revState = { q: "", sort: "date", open: {} };

const revAll = () => ((DATA.sheets.completed || {}).rows || [])
  .filter((r) => r.notes && String(r.notes).length >= MIN_REVIEW);

const wordCount = (s) => String(s).trim().split(/\s+/).length;

const REV_SORTS = {
  date: { label: "Newest first", cmp: (a, b) => String(b.date || "").localeCompare(String(a.date || "")) },
  oldest: { label: "Oldest first", cmp: (a, b) => String(a.date || "").localeCompare(String(b.date || "")) },
  rating: { label: "Highest rated", cmp: (a, b) => (b.rating ?? 0) - (a.rating ?? 0) },
  longest: { label: "Longest", cmp: (a, b) => String(b.notes).length - String(a.notes).length },
};

function revFiltered() {
  const q = revState.q.toLowerCase().trim();
  let rows = revAll();
  if (q) {
    rows = rows.filter((r) =>
      String(r.game).toLowerCase().includes(q) ||
      String(r.notes).toLowerCase().includes(q) ||
      String(r.platform || "").toLowerCase().includes(q) ||
      String(r.franchise || "").toLowerCase().includes(q));
  }
  return rows.slice().sort((REV_SORTS[revState.sort] || REV_SORTS.date).cmp);
}

// Highlight the search term inside the review body.
function revHighlight(text, q) {
  const safe = escapeHtml(text);
  if (!q) return safe;
  const needle = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(needle, "gi"), (m) => `<mark>${m}</mark>`);
}

function revCard(r, i) {
  const cs = coverSrc(ENRICH[r._k], "cover_big");
  const cover = cs
    ? `<img class="rev-cover" loading="lazy" src="${escapeHtml(cs)}" alt="">`
    : `<div class="rev-cover ph">🎮</div>`;
  const open = !!revState.open[r._k];
  const words = wordCount(r.notes);
  const bits = [r.platform, r.releaseYear, r.date ? fmtDate(r.date) : null]
    .filter(Boolean).map((x) => escapeHtml(String(x))).join(" · ");
  const score = r.rating != null
    ? `<span class="rev-score ${ratingClass(r.rating)}">${Math.round(r.rating * 100)}</span>` : "";
  const critic = r.criticScore != null
    ? `<span class="rev-critic">critics ${Math.round(r.criticScore * 100)}</span>` : "";
  const time = r.playTime != null ? `<span class="rev-time">⏱ ${fmtHours(r.playTime)}</span>` : "";
  return `<article class="rev${open ? " open" : ""}">
    <button class="rev-art" data-rk="${escapeHtml(String(r._k || ""))}" aria-label="Open ${escapeHtml(String(r.game))}">${cover}</button>
    <div class="rev-body">
      <header>
        <button class="rev-title" data-rk="${escapeHtml(String(r._k || ""))}">${escapeHtml(String(r.game))}</button>
        ${score}
      </header>
      <div class="rev-meta">${bits}${critic ? " · " + critic : ""}${time ? " · " + time : ""} · ${words.toLocaleString()} words</div>
      <div class="rev-text" id="rt${i}">${revHighlight(String(r.notes), revState.q.trim())}</div>
      <button class="rev-more" data-more="${escapeHtml(String(r._k || ""))}">${open ? "Show less" : "Read more"}</button>
    </div>
  </article>`;
}

// Enrichment lands after the first render, so covers start as placeholders.
// Patch them in place — a re-render would recreate every <img> and flash.
function patchReviewCovers() {
  const host = $("#reviews");
  if (!host) return;
  host.querySelectorAll(".rev-art").forEach((el) => {
    const ph = el.querySelector(".rev-cover.ph");
    if (!ph) return;
    const cs = coverSrc(ENRICH[el.dataset.rk], "cover_big");
    if (!cs) return;
    const img = document.createElement("img");
    img.className = "rev-cover"; img.loading = "lazy"; img.alt = ""; img.src = cs;
    ph.replaceWith(img);
  });
}

function renderReviews() {
  const host = $("#reviews");
  if (!DATA) return;
  const all = revAll();
  const rows = revFiltered();
  const totalWords = all.reduce((a, r) => a + wordCount(r.notes), 0);

  host.innerHTML =
    `<div class="rev-head">
      <h1>Reviews</h1>
      <p>${all.length.toLocaleString()} reviews · ${totalWords.toLocaleString()} words you’ve written about games.</p>
      <div class="rev-controls">
        <input id="revQ" type="search" placeholder="Search reviews…" value="${escapeHtml(revState.q)}" autocomplete="off">
        <label class="ctl">Sort
          <select id="revSort">${Object.entries(REV_SORTS).map(([k, v]) =>
            `<option value="${k}"${k === revState.sort ? " selected" : ""}>${escapeHtml(v.label)}</option>`).join("")}</select>
        </label>
        <span class="muted">${rows.length.toLocaleString()} shown</span>
      </div>
    </div>` +
    (rows.length
      ? `<div class="rev-list">${rows.map(revCard).join("")}</div>`
      : emptyState("No reviews match", "Try a different search.", null));

  const q = $("#revQ");
  q.oninput = () => {
    revState.q = q.value;
    const at = q.selectionStart;
    renderReviews();
    const q2 = $("#revQ");
    q2.focus();
    q2.setSelectionRange(at, at);
  };
  $("#revSort").onchange = (e) => { revState.sort = e.target.value; renderReviews(); };
  host.querySelectorAll("[data-rk]").forEach((el) => {
    el.onclick = () => {
      const row = revAll().find((r) => String(r._k || "") === el.dataset.rk);
      if (row) openDrawer(row, "completed");
    };
  });
  host.querySelectorAll("[data-more]").forEach((el) => {
    el.onclick = () => {
      const k = el.dataset.more;
      revState.open[k] = !revState.open[k];
      // Toggle in place — a full re-render would recreate every cover and flash.
      const art = el.closest(".rev");
      art.classList.toggle("open", !!revState.open[k]);
      el.textContent = revState.open[k] ? "Show less" : "Read more";
    };
  });
}
