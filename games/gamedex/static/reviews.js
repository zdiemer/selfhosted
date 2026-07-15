"use strict";

/* Your reviews — a Stats section that reads your own writing back to you.

   745 of the completed rows carry a long-form write-up in `notes`; nothing else in the
   app looks at them in aggregate. This turns that ~85k words into four panels: how much
   you've written, the words that ride along with your best and worst scores, the fact
   that you write more about games you love, and the reviews whose PROSE doesn't match the
   NUMBER you gave — a glowing paragraph on a 5/10, or a gripe-fest on a game you scored 100.

   Self-contained: no server, no bundled lexicon. "Sentiment" is learned from your own
   ratings (the words you reach for when you rate high vs low), which is why the mismatch
   pass uses LEAVE-ONE-OUT — a review's own rating must not colour the words it's judged by,
   or every review would look like it agrees with itself. Loaded after app.js + charts.js,
   so statPanel/statCard/barsH/barsV/gameLink/foldText/escapeHtml are all in scope. */

const RV_MIN_REVIEWS = 40;   // below this it's a novelty, not a dataset
const RV_WORD_MIN = 8;       // a word needs this many reviews behind it to earn a polarity
const RV_LIFT_MIN = 12;      // and this many for the descriptive high/low-score lists

// A plain stopword list — function words plus the vocabulary EVERY game review uses
// ("game", "play"), which would otherwise dominate every panel while saying nothing.
const RV_STOP = new Set((
  "the a an and or but of to in on at for with is was are be been being it its this that " +
  "these those i you he she they we me my your our their his her them there here as so if " +
  "then than too very just about into over out up down not no nor only own same s t can " +
  "will would could should did does do done have has had having what which who whom whose " +
  "when where why how all any both each few more most other some such off again further " +
  "once because while during before after above below between through against " +
  "game games play played playing plays player gameplay title game's im ive dont didnt cant " +
  "one two get got go going get gets im really lot pretty thing things able bit way well good " +
  "much many also still even though although get like feel felt makes make made time"
).split(/\s+/));

const rvTokens = (s) =>
  foldText(s).replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter((w) => w.length > 2 && !RV_STOP.has(w));

function reviewStats(rows) {
  const R = [];
  for (const r of rows) {
    const raw = r.review != null ? r.review : r.notes;   // completed sheet aliases notes -> review
    const text = raw == null ? "" : String(raw).trim();
    if (text.length > 3 && r.rating != null) R.push({ row: r, name: String(r.game || r.title || ""), rating: r.rating, text });
  }
  if (R.length < RV_MIN_REVIEWS) return "";

  // ---- headline corpus stats -------------------------------------------------
  const allWords = R.flatMap((r) => foldText(r.text).replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean));
  const totWords = allWords.length;
  const vocab = new Set(allWords).size;
  const pages = Math.round(totWords / 300);           // ~a paperback page
  const readMin = Math.round(totWords / 200);         // read-aloud, 200 wpm
  const novels = totWords / 90000;                    // a typical novel
  const avgLen = Math.round(totWords / R.length);
  const longest = R.reduce((a, r) => (r.text.length > a.text.length ? r : a), R[0]);

  const tiles =
    statCard(R.length, "Reviews written", "", "", { icon: "i-star", sub: `${Math.round(R.length / rows.length * 100)}% of what you've beaten` }) +
    statCard(totWords, "Words", "", "", { icon: "i-trend", sub: `≈ ${novels.toFixed(1)} novels` }) +
    statCard(pages, "Pages", "", "", { sub: "at 300 words a page" }) +
    statCard(vocab, "Unique words", "", "", { sub: `${avgLen} per review on average` }) +
    statCard(readMin >= 90 ? +(readMin / 60).toFixed(1) : readMin, "To read aloud", "", readMin >= 90 ? "h" : "m", { icon: "i-clock" }) +
    statCard(longest.text.split(/\s+/).length, "Longest review", "", "w", { sub: escapeHtml(longest.name) });

  // ---- the words behind your scores (descriptive) ---------------------------
  const global = R.reduce((a, r) => a + r.rating, 0) / R.length;
  const wStat = new Map();                            // word -> {n reviews, sum of their ratings}
  for (const r of R) for (const w of new Set(rvTokens(r.text))) {
    const e = wStat.get(w) || { n: 0, sum: 0 }; e.n++; e.sum += r.rating; wStat.set(w, e);
  }
  const lift = [...wStat.entries()]
    .filter(([, e]) => e.n >= RV_LIFT_MIN)
    .map(([w, e]) => ({ w, n: e.n, avg: e.sum / e.n, d: (e.sum / e.n - global) }));
  lift.sort((a, b) => b.d - a.d);
  const liftRow = (s) => ({ label: s.w, value: Math.round(s.d * 100),
    tip: `“${s.w}” — ${Math.round(s.avg * 100)}% average across ${s.n} reviews` });
  const liftData = [...lift.slice(0, 8), ...lift.slice(-8)].map(liftRow);

  // ---- length vs rating ------------------------------------------------------
  const lenBuckets = [["Hated (<50)", -1, .5], ["Meh (50–69)", .5, .7], ["Liked (70–84)", .7, .85], ["Loved (85+)", .85, 1.01]];
  const lenData = lenBuckets.map(([label, lo, hi]) => {
    const g = R.filter((r) => r.rating >= lo && r.rating < hi);
    return { label, value: g.length ? Math.round(g.reduce((a, r) => a + r.text.split(/\s+/).length, 0) / g.length) : 0 };
  });

  // ---- sentiment vs score mismatch (leave-one-out) --------------------------
  // Each word's polarity is its reviews' average rating minus the global mean. To score a
  // review, we recompute every word WITHOUT that review, so its own rating can't leak in.
  const scored = [];
  for (const r of R) {
    let acc = 0, k = 0;
    for (const w of new Set(rvTokens(r.text))) {
      const e = wStat.get(w); if (!e) continue;
      const n = e.n - 1; if (n < RV_WORD_MIN - 1) continue;       // drop this review, need evidence left
      acc += (e.sum - r.rating) / n - global; k++;
    }
    if (k >= 5) scored.push({ ...r, senti: acc / k });            // mean word polarity
  }
  let mismPanels = [];
  if (scored.length >= 30) {
    const N = scored.length;
    // Pearson r, just to tell you how tightly your prose and your scores usually agree.
    const mx = scored.reduce((a, r) => a + r.senti, 0) / N, my = scored.reduce((a, r) => a + r.rating, 0) / N;
    let num = 0, dx = 0, dy = 0;
    for (const r of scored) { num += (r.senti - mx) * (r.rating - my); dx += (r.senti - mx) ** 2; dy += (r.rating - my) ** 2; }
    const corr = dx > 1e-9 && dy > 1e-9 ? num / Math.sqrt(dx * dy) : 0;

    // Rank-based, NOT residual-from-a-fit. A fit is compressed, so it flags your 100s and your
    // 10s every time purely because their words can't be as extreme as the number — it just
    // re-lists your rating extremes. Comparing each review's sentiment PERCENTILE to its rating
    // PERCENTILE is scale-free: among all your 100s, only the least enthusiastic writeups stand
    // out, which is the actually-surprising thing. "Words rank like a Y" reads the sentiment
    // percentile back off your own rating distribution, so it stays on the 0-100 scale.
    const pctOf = (key) => {
      const vals = scored.map((r) => r[key]);
      return (v) => { let lt = 0, eq = 0; for (const x of vals) { if (x < v) lt++; else if (x === v) eq++; } return (lt + (eq + 1) / 2) / (N + 1); };
    };
    const ratingPct = pctOf("rating"), sentiPct = pctOf("senti");
    const sortedR = scored.map((r) => r.rating).sort((a, b) => a - b);
    const quantile = (p) => sortedR[Math.max(0, Math.min(N - 1, Math.round(p * (N - 1))))];
    for (const r of scored) { r.rp = ratingPct(r.rating); r.sp = sentiPct(r.senti); r.gap = r.rp - r.sp; r.wordsAs = quantile(r.sp); }
    const flagged = scored.filter((r) => Math.abs(r.gap) > 0.33);

    const bar = (r) => ({ label: r.name, value: Math.round(Math.abs(r.rating - r.wordsAs) * 100),
      link: gameLink(r.row, "completed"),
      tip: `You rated it ${Math.round(r.rating * 100)} — its words rank like a ${Math.round(r.wordsAs * 100)}` });
    const warmer = flagged.filter((r) => r.gap < 0).sort((a, b) => a.gap - b.gap).slice(0, 8).map(bar);
    const harsher = flagged.filter((r) => r.gap > 0).sort((a, b) => b.gap - a.gap).slice(0, 8).map(bar);

    if (warmer.length) mismPanels.push(statPanel("Kinder in words than in score", barsH(warmer, { fmt: (v) => v + " pts warmer" }), "",
      `Your writing tracks your scores at r=${corr.toFixed(2)}; these ${flagged.length} drift furthest. You wrote about ` +
      `these more warmly than most games you scored this low — judged by rank, so it isn't just your bottom scores.`));
    if (harsher.length) mismPanels.push(statPanel("Harsher in words than in score", barsH(harsher, { fmt: (v) => v + " pts colder" }), "",
      "Scored high, but the review reads cooler than most games you rated this well — a nitpicker's love. Click one to reread it."));
  }

  const panels = [
    statPanel("The words behind your scores", barsH(liftData, { fmt: (v) => (v > 0 ? "+" : "") + v, diverging: true }), "wide",
      "Each word's bar is how far your average score swings on reviews that use it, above (green) or below (red) your overall average. Learned from your writing, not a dictionary."),
    statPanel("The more you love it, the more you write", barsV(lenData, { tone: "good", fmt: (v) => v + " words" }), "",
      "Average review length by how you rated the game."),
    ...mismPanels,
  ];

  return `<h2 class="stat-sec"><span>Your reviews</span><i>${R.length}</i></h2>` +
    `<div class="stat-cards">${tiles}</div>` +
    `<div class="stat-grid">${panels.join("")}</div>`;
}
