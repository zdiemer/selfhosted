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

/* A title reduced to what a typo would actually change. Roman numerals become
   digits, "&" becomes "and", accents are stripped, and everything that isn't a
   letter or a digit goes — so only a real letter-level difference survives. */
const _ROMAN = { i: "1", ii: "2", iii: "3", iv: "4", v: "5", vi: "6", vii: "7",
                 viii: "8", ix: "9", x: "10", xi: "11", xii: "12", xiii: "13" };
function hzTitleKey(t) {
  return String(t || "")
    .toLowerCase()
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    // Editions and tags in brackets are a different EDITION, not a different
    // spelling: "Resident Evil 4" vs "Resident Evil 4 [VR]".
    .replace(/[[(][^\])]*[\])]/g, " ")
    // A leading article is a cataloguing convention, not a typo: IGDB files it as
    // "A Total War Saga: Troy", the sheet doesn't.
    .replace(/^(a|an|the)\s+/, "")
    .replace(/&/g, "and")
    .replace(/[×x]/g, "x")
    .split(/\s+/)
    .map((w) => {
      const bare = w.replace(/[^a-z0-9]/g, "");
      return _ROMAN[bare] || bare;
    })
    .join("")
    .replace(/[^a-z0-9]/g, "");
}

// Levenshtein, bounded. We only care whether it's 1-2 edits, so bail out early
// rather than filling a 60x60 matrix for every one of 14,752 titles.
function hzEditDistance(a, b, max = 3) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (cur[j] < best) best = cur[j];
    }
    if (best > max) return max + 1;       // no path back under the threshold
    prev = cur;
  }
  return prev[b.length];
}

// Edit distance, capped: we only ever ask "is this within 2 edits", so bail early
// rather than compute the full matrix for a pair that's obviously miles apart.
function hzEdit(a, b, max = 3) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (cur[j] < best) best = cur[j];
    }
    if (best > max) return max + 1;      // whole row already over budget
    prev = cur;
  }
  return prev[b.length];
}

// ---- match confidence ----------------------------------------------------
// Every automatic match already carried a score and nobody ever saw it.
// MatchValidator.match_score is 0-15: 5 for matching the title at all, 5 MORE if
// that title was exact, then +1 each for platform, release date, publisher,
// developer and franchise. So >= 10 means the title matched exactly; below that
// the matcher settled for something that merely looked similar.
const CONF_EXACT = 10;

const hzConf = (r) => {
  const e = ENRICH[r._k];
  if (!e || e.manualMatch) return null;        // you picked it by hand; not ours to doubt
  if (!e.igdbId && !e.source) return null;     // nothing matched at all — that's "nometa"
  return typeof e.confidence === "number" ? e.confidence : null;
};

// The one thing that makes a bad match obvious: what it matched you TO.
const hzConfDetail = (r) => {
  const e = ENRICH[r._k] || {};
  const c = hzConf(r);
  const name = e.name && hzNorm(e.name) !== hzNorm(r.title)
    ? `matched \u201c${e.name}\u201d` : "same title";
  return `${name} \u00b7 ${c}/15 \u00b7 ${e.source || "igdb"}`;
};

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
    id: "titleonly", severity: "error", sheet: "games",
    title: "Matched on a fuzzy title and nothing else",
    why: "Confidence 5/15 or less: the matcher accepted a title that was merely SIMILAR, and nothing "
       + "corroborated it — not the platform, not the release year, not the publisher, developer or "
       + "franchise. These are where a wrong cover, a wrong score or a wrong launch link comes from. "
       + "Open one, check the matched name against yours, and pin the right game with Match manually.",
    find: () => hzGames().filter((r) => { const c = hzConf(r); return c != null && c <= 5; })
      .sort((a, b) => hzConf(a) - hzConf(b)),
    detail: hzConfDetail,
  },
  {
    id: "lowconf", severity: "warn", sheet: "games",
    title: "Low-confidence metadata match",
    why: "Confidence 6-9/15: the title was a fuzzy match rather than an exact one, though something else "
       + "agreed (platform, year, publisher…). Usually right — a subtitle or a \u00ae the sheet spells "
       + "differently — but this is the pile worth spot-checking.",
    find: () => hzGames().filter((r) => { const c = hzConf(r); return c != null && c > 5 && c < CONF_EXACT; })
      .sort((a, b) => hzConf(a) - hzConf(b)),
    detail: hzConfDetail,
  },
  {
    id: "misspelled", severity: "warn", sheet: "games",
    title: "Title may be misspelled",
    why: "The sheet's title and IGDB's differ by a letter or two — close enough to be the same game, "
       + "far enough apart that one of them is a typo. Note that it isn't always yours: IGDB spells "
       + "Slayers X as 'Vengance'. Punctuation, roman numerals, bracketed editions and leading "
       + "articles are normalised away first, so what's left is a genuine letter-level difference.",
    find: () => hzGames().filter((r) => {
      const e = ENRICH[r._k];
      if (!e || !e.name || e.manualMatch) return false;
      // Only where the match is CONFIDENT: a low-confidence match differing by two
      // characters is a bad match, not a typo, and it's already flagged as one.
      if (typeof e.confidence === "number" && e.confidence < CONF_EXACT) return false;
      /* Compare on LETTERS, not on punctuation and numerals. Raw edit distance
         flagged 665 games, and almost none were typos:

           "Helldivers II"          vs "Helldivers 2"            roman numeral
           "Command & Conquer: X"   vs "Command & Conquer X"     a colon
           "Invincible VS"          vs "Invincible Vs."          a full stop

         Those are style differences between two catalogues, not misspellings.
         Normalise them away and what's left is a genuine letter-level typo. */
      const a = hzTitleKey(r.title), b = hzTitleKey(e.name);
      if (!a || !b || a === b) return false;
      const d = hzEditDistance(a, b);
      return d > 0 && d <= 2 && Math.min(a.length, b.length) >= 10;
    }),
    detail: (r) => `sheet: "${r.title}" · IGDB: "${(ENRICH[r._k] || {}).name}"`,
  },
  {
    id: "incompletecol", severity: "info", sheet: "games",
    title: "Collections you've only partly finished",
    why: "A compilation where you've beaten some of the games inside but never marked the "
       + "collection itself complete. Either there's more to play, or the parent row needs ticking.",
    find: () => {
      if (typeof buildCollections !== "function") return [];
      buildCollections();
      const out = [];
      for (const c of (typeof collectionAll === "function" ? collectionAll() : [])) {
        if (!c.parent || c.complete) continue;
        if (!c.members.length) continue;
        out.push(c.parent);
      }
      return out;
    },
    detail: (r) => {
      const c = typeof collectionOfParent === "function" ? collectionOfParent(r) : null;
      return c ? `${c.members.length} of its games finished, collection not marked complete` : "";
    },
  },
  {
    id: "typo", severity: "warn", sheet: "games",
    title: "Possible typo in the title",
    why: "The title matched IGDB, but not EXACTLY — and what IGDB calls it is only a character or two "
       + "away from what the sheet calls it. That gap is almost always a typo on our side rather than a "
       + "different game. (A genuinely different game doesn't land one edit away from yours.) Upstream "
       + "does this with a spellchecker and a dictionary; we don't need one, because we already store "
       + "what IGDB thinks the game is called.",
    find: () => hzGames().filter((r) => {
      const e = ENRICH[r._k];
      if (!e || !e.name || e.manualMatch) return false;
      if (typeof e.confidence !== "number" || e.confidence >= CONF_EXACT) return false;  // exact title: nothing to fix
      const a = hzNorm(r.title), b = hzNorm(e.name);
      if (!a || !b || a === b) return false;
      const d = hzEdit(a, b);
      // 1-2 edits on a title of reasonable length. Beyond that it's a subtitle or
      // a different edition, not a slip of the keyboard.
      return d > 0 && d <= 2 && Math.min(a.length, b.length) >= 6;
    }).sort((a, b) => hzEdit(hzNorm(a.title), hzNorm((ENRICH[a._k] || {}).name || ""))
                    - hzEdit(hzNorm(b.title), hzNorm((ENRICH[b._k] || {}).name || ""))),
    detail: (r) => {
      const e = ENRICH[r._k] || {};
      return `IGDB calls it \u201c${e.name}\u201d`;
    },
  },
  {
    id: "incompletecoll", severity: "info", sheet: "games",
    title: "Collections you started but never finished",
    why: "A compilation with games finished inside it, but the compilation itself isn't marked complete. "
       + "Either there's more of it to play, or the parent row needs ticking.",
    find: () => {
      if (typeof buildCollections !== "function") return [];
      buildCollections();
      const out = [];
      for (const c of (typeof collectionsAll === "function" ? collectionsAll() : [])) {
        if (c.parent && !c.parent.completed && c.members.length) out.push(c.parent);
      }
      return out;
    },
    detail: (r) => {
      const c = typeof collectionOfParent === "function" ? collectionOfParent(r) : null;
      return c ? `${c.members.length} game${c.members.length === 1 ? "" : "s"} inside it finished` : "";
    },
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
