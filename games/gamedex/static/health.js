"use strict";

/* Data health — what's wrong with the spreadsheet.

   Ported from the validation/statistics selectors in zdiemer/GamesMaster and
   zdiemer/GamePicker (potential_duplicates, missing_playtime, completed_ordering,
   unknown_playability, hltb_mismatch, largest_rating_differences, …).

   Each check is a question with a list of offending rows behind it. Every row is
   clickable, so a check is a work queue: open the game, see what's missing, fix
   it in Dropbox, and it disappears on the next poll.

   Loaded after app.js; shares its globals (DATA, ENRICH, openDrawer, …). */

const healthState = { open: null };

const hzGames = () => ((DATA.sheets.games || {}).rows || []).filter((r) => r.title);
const hzDone = () => ((DATA.sheets.completed || {}).rows || []);

// Same normalisation the matcher uses, far enough to spot near-duplicates.
const hzNorm = (s) => String(s || "").toLowerCase()
  .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
  .replace(/[^a-z0-9]/g, "");

// severity: "error" = almost certainly wrong · "warn" = probably worth a look ·
// "info" = just a gap you may not care about.
const HEALTH_CHECKS = [
  {
    id: "dupes", severity: "warn", sheet: "games",
    title: "Possible duplicate rows",
    why: "Same normalised title on the same platform. Sometimes legitimate (two editions), often a double-entry.",
    find: () => {
      const seen = new Map();
      for (const r of hzGames()) {
        const k = `${hzNorm(r.title)}|${r.platform || ""}`;
        if (!seen.has(k)) seen.set(k, []);
        seen.get(k).push(r);
      }
      return [...seen.values()].filter((g) => g.length > 1).flat();
    },
  },
  {
    id: "order", severity: "error", sheet: "games",
    title: "Finished before it was started",
    why: "Date Completed is earlier than Date Started — one of the two dates is wrong.",
    find: () => hzGames().filter((r) => r.dateStarted && r.dateCompleted && r.dateCompleted < r.dateStarted),
  },
  {
    id: "future", severity: "error", sheet: "games",
    title: "Completed in the future",
    why: "Date Completed is after today.",
    find: () => {
      const today = new Date().toISOString().slice(0, 10);
      return hzGames().filter((r) => r.dateCompleted && r.dateCompleted > today);
    },
  },
  {
    id: "donenodate", severity: "warn", sheet: "games",
    title: "Completed, but no completion date",
    why: "Marked Completed with no Date Completed — it won't count toward any challenge, which all key off that date.",
    find: () => hzGames().filter((r) => r.completed && !r.dateCompleted),
  },
  {
    id: "donenotime", severity: "info", sheet: "games",
    title: "Completed, but no completion time",
    why: "Marked Completed with no Completion Time, so it's missing from the hours-played totals.",
    find: () => hzGames().filter((r) => r.completed && r.completionTime == null),
  },
  {
    id: "stalled", severity: "info", sheet: "games",
    title: "Started, never finished, not marked as playing",
    why: "Has a Date Started but isn't Completed and has no Playing Status — abandoned, or just untracked?",
    find: () => hzGames().filter((r) => r.dateStarted && !r.completed && !r.playingStatus),
  },
  {
    id: "playingdone", severity: "error", sheet: "games",
    title: "Completed, but still marked as playing",
    why: "Completed and a Playing Status at the same time — it'll show up in Now Playing forever.",
    find: () => hzGames().filter((r) => r.completed && r.playingStatus),
  },
  {
    id: "unknownplay", severity: "info", sheet: "games",
    title: "Playability unknown",
    why: "Playable is Unknown, so these are excluded from every challenge and from the picker.",
    find: () => hzGames().filter((r) => r.playable === "Unknown"),
  },
  {
    id: "ownednoprice", severity: "info", sheet: "games",
    title: "Owned, but no purchase price",
    why: "Missing from the spend totals and from the buy→finish gap.",
    find: () => hzGames().filter((r) => r.owned && r.purchasePrice == null),
  },
  {
    id: "wishowned", severity: "warn", sheet: "games",
    title: "Wishlisted and already owned",
    why: "You own it — it shouldn't still be on the wishlist.",
    find: () => hzGames().filter((r) => r.wishlisted && r.owned),
  },
  {
    id: "hltbgap", severity: "warn", sheet: "games",
    title: "Your playtime is wildly off HowLongToBeat",
    why: "Your Completion Time differs from HLTB's main story by more than 3× — likely a units slip (minutes for hours) or a typo.",
    find: () => hzGames().filter((r) => {
      const e = ENRICH[r._k];
      const mine = r.completionTime, theirs = e && e.hltbMain;
      if (!mine || !theirs || mine < 0.5 || theirs < 0.5) return false;
      const ratio = mine > theirs ? mine / theirs : theirs / mine;
      return ratio >= 3;
    }),
    detail: (r) => {
      const e = ENRICH[r._k] || {};
      return `you ${fmtHours(r.completionTime)} · HLTB ${fmtHours(e.hltbMain)}`;
    },
  },
  {
    id: "criticgap", severity: "info", sheet: "completed",
    title: "Biggest disagreements with the critics",
    why: "Not an error — just where your score is furthest from the critics'. Worth a sanity check for typos.",
    find: () => hzDone()
      .filter((r) => r.rating != null && r.criticScore != null && Math.abs(r.rating - r.criticScore) >= 0.35)
      .sort((a, b) => Math.abs(b.rating - b.criticScore) - Math.abs(a.rating - a.criticScore)),
    detail: (r) => {
      const d = Math.round((r.rating - r.criticScore) * 100);
      return `you ${Math.round(r.rating * 100)} · critics ${Math.round(r.criticScore * 100)} · ${d > 0 ? "+" : ""}${d}`;
    },
  },
  {
    id: "nometa", severity: "info", sheet: "games",
    title: "No metadata from any source",
    why: "No IGDB, no fallback, no cover — usually a title that needs correcting, or a manual mapping.",
    find: () => hzGames().filter((r) => {
      const e = ENRICH[r._k];
      return !e || (!e.igdbId && !e.source && !e.cover && !e.coverUrl);
    }),
  },
  {
    id: "nopriority", severity: "info", sheet: "games",
    title: "No priority set",
    why: "Priority drives the picker and the challenge candidate pool; unset rows are treated as lowest.",
    find: () => hzGames().filter((r) => !r.completed && (r.priority == null || r.priority === "")),
  },
];

const SEV = {
  error: { label: "Error", cls: "sev-error" },
  warn: { label: "Warning", cls: "sev-warn" },
  info: { label: "Gap", cls: "sev-info" },
};

let _healthResults = null;
function healthResults() {
  if (_healthResults) return _healthResults;
  return (_healthResults = HEALTH_CHECKS.map((c) => ({ c, rows: c.find() })));
}
const resetHealth = () => { _healthResults = null; };

function healthRowHtml(check, r, i) {
  const title = String(r.title || r.game || "");
  const bits = [r.platform, r.releaseYear].filter((x) => x != null && x !== "")
    .map((x) => escapeHtml(String(x))).join(" · ");
  const extra = check.detail ? check.detail(r) : "";
  return `<button class="hz-row" data-hc="${check.id}" data-hi="${i}">
    <span class="hz-row-t">${escapeHtml(title)}</span>
    <span class="hz-row-m">${bits}</span>
    ${extra ? `<span class="hz-row-x">${escapeHtml(extra)}</span>` : ""}
  </button>`;
}

const HZ_SHOWN = 40;

function renderHealth() {
  const host = $("#health");
  if (!DATA) return;
  const results = healthResults();
  const errs = results.filter((x) => x.c.severity === "error" && x.rows.length).length;
  const warns = results.filter((x) => x.c.severity === "warn" && x.rows.length).length;
  const total = results.reduce((a, x) => a + x.rows.length, 0);

  host.innerHTML =
    `<div class="hz-head">
      <h1>Data health</h1>
      <p>${total.toLocaleString()} rows across ${results.filter((x) => x.rows.length).length} checks want a second look.
         Fix them in the spreadsheet and they'll clear on the next Dropbox poll.</p>
      <div class="hz-summary">
        <span class="hz-pill sev-error">${errs} error${errs !== 1 ? "s" : ""}</span>
        <span class="hz-pill sev-warn">${warns} warning${warns !== 1 ? "s" : ""}</span>
        <span class="hz-pill sev-info">${results.filter((x) => x.c.severity === "info" && x.rows.length).length} gaps</span>
      </div>
    </div>
    <div class="hz-list">` +
    results.map(({ c, rows }) => {
      const open = healthState.open === c.id;
      const sev = SEV[c.severity];
      return `<section class="hz-check${rows.length ? "" : " clean"}${open ? " open" : ""}">
        <button class="hz-check-head" data-toggle="${c.id}">
          <span class="hz-pill ${sev.cls}">${sev.label}</span>
          <span class="hz-check-t">${escapeHtml(c.title)}</span>
          <span class="hz-count">${rows.length ? rows.length.toLocaleString() : "✓ clean"}</span>
          <span class="hz-caret">${open ? "▾" : "▸"}</span>
        </button>
        ${open ? `<div class="hz-body">
          <p class="hz-why">${escapeHtml(c.why)}</p>
          <div class="hz-rows">${rows.slice(0, HZ_SHOWN).map((r, i) => healthRowHtml(c, r, i)).join("")}</div>
          ${rows.length > HZ_SHOWN ? `<p class="hz-more">Showing the first ${HZ_SHOWN} of ${rows.length.toLocaleString()}.</p>` : ""}
        </div>` : ""}
      </section>`;
    }).join("") + `</div>`;

  host.querySelectorAll("[data-toggle]").forEach((el) => {
    el.onclick = () => {
      healthState.open = healthState.open === el.dataset.toggle ? null : el.dataset.toggle;
      renderHealth();
    };
  });
  host.querySelectorAll("[data-hc]").forEach((el) => {
    el.onclick = () => {
      const res = healthResults().find((x) => x.c.id === el.dataset.hc);
      if (!res) return;
      const row = res.rows[+el.dataset.hi];
      if (row) openDrawer(row, res.c.sheet);
    };
  });
}
