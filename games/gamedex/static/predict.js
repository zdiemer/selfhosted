"use strict";

/* Predicted rating — what you'd probably score a game you haven't played.

   A ridge regression, trained in the browser on your own rated completions. No library
   and no server: ~1,700 rows and two dozen features is a small matrix solve.

   WHAT IT LOOKS AT

     What you think    your average score for games sharing this franchise / developer /
                       publisher / genre / platform (the sheet's own columns), and the
                       same again over IGDB's MULTI-VALUED tags — a game is rarely one
                       genre, and "Metroidvania, Platformer, Adventure" says more than
                       whichever single word the sheet settled on. Themes and game modes
                       too: you have opinions about horror, and about multiplayer.

     What others think two independent verdicts, each regressed onto YOUR scale: the
                       critics, and GameFAQs' player score. They are not the same signal —
                       critics and players disagree constantly, and the gap between them
                       is itself a feature. The player score matters more than it looks:
                       across the games we actually predict for, critics cover 59% and
                       players cover 87%, so for a quarter of the backlog the players are
                       the ONLY outside opinion there is.

     What kind of game how long it is (HowLongToBeat), how old it is, and how many critics
                       bothered to review it — a decent proxy for how big a release it was.

   WHY IT ISN'T SELF-CONGRATULATORY NONSENSE

   1. SHRINKAGE. "You rated the one Bloodborne you played 100%" is not evidence that you'd
      rate every FromSoftware game 100%. Each group average is pulled toward your global
      mean in proportion to how little data backs it:

          estimate = (sum + k * globalMean) / (n + k)

   2. LEAVE-ONE-OUT, so it can't cheat. Building the training row for a game you finished
      REMOVES that game from its own group averages. Without this the model sees its own
      answer in its own features — "your average Castlevania score" would already contain
      the Castlevania we're asking about — and it would look brilliant and predict nothing.

   3. LEARNED WEIGHTS, not guessed ones — fitted by ridge rather than hand-tuned.

   4. HONEST SCORING. Five-fold cross-validation, with every encoder (the group averages,
      both calibrations, the scaler) rebuilt from the TRAINING fold alone — score a model
      against games whose ratings shaped its own features and it will flatter itself.
      Measured that way it lands near 9.2 points of average error, against 9.8 for the old
      five-feature model, 10.3 for simply quoting the critics, and 12.4 for guessing your
      average every time.

      That is about one notch on a scale you record in notches of five, and it is close to
      the practical floor for these features: your ratings have a standard deviation of
      16.5 points, and the best outside signal available correlates 0.64 with you. Things
      that were tried and did NOT beat this, on the same cross-validation: a weight per
      individual tag instead of shrunk group averages (300 free parameters overfits 1,700
      games), gradient-boosted trees, fitting the median instead of the mean, k-nearest
      taste neighbours, and feeding it your own wishlist flags.

   Loaded after app.js; shares its globals. */

const PRIOR_K = 3;               // evidence needed before a group average is trusted
const RIDGE = 0.05;              // regularisation (on standardised features)
const MIN_HISTORY = 60;          // below this we simply don't have enough to say
const CV_FOLDS = 5;

// The sheet's single-valued columns.
const PRED_FEATURES = ["franchise", "developer", "publisher", "genre", "platform"];
// IGDB's multi-valued tags. A game carries several of each, and each one gets a vote.
const PRED_MULTI = {
  igdbGenre: (r) => (ENRICH[r._k] || {}).genres || [],
  igdbTheme: (r) => (ENRICH[r._k] || {}).themes || [],
  igdbMode: (r) => (ENRICH[r._k] || {}).gameModes || [],
  igdbDev: (r) => (ENRICH[r._k] || {}).developers || [],
  igdbPub: (r) => (ENRICH[r._k] || {}).publishers || [],
  igdbFran: (r) => (ENRICH[r._k] || {}).franchises || [],
  // Keywords, perspective and engine. Measured, honestly: worth about 0.03 points on the
  // same cross-validation — consistently positive across every seed, but small. They earn
  // their place only because the data is already in the payload for the facets; if it cost
  // a fetch it would not be worth it. The fine-grained vocabulary I expected to be a big win
  // (metroidvania, soulslike) turns out to be mostly saying what genre already said.
  igdbKeyword: (r) => (ENRICH[r._k] || {}).keywords || [],
  igdbPersp: (r) => (ENRICH[r._k] || {}).perspectives || [],
  igdbEngine: (r) => (ENRICH[r._k] || {}).engines || [],
};
const PRED_MULTI_KEYS = Object.keys(PRED_MULTI);
const pnorm = (s) => String(s).trim().toLowerCase();

// The critic score. criticOf() already walks Metacritic → the sheet → IGDB → GameRankings;
// the completed sheet keeps its own copy under a different name, so fall back to that.
const predCritic = (row) => {
  const c = criticOf(row);
  if (c != null) return c;
  return row.criticScore != null ? row.criticScore : null;
};
// What PLAYERS thought — a different opinion, not a second helping of the same one. The
// GameFAQs column lives on the GAMES sheet, so a completed row has to reach across for it;
// that join is the reason this signal sat unused, and it is the single biggest win here.
const predPlayers = (row) => {
  const e = ENRICH[row._k] || {};
  if (e.userRating != null) return e.userRating;
  if (e.vnRating != null) return e.vnRating;
  if (row.gamefaqsUserRating != null) return row.gamefaqsUserRating;
  const g = typeof rowsByK === "function" ? rowsByK().games.get(row._k) : null;
  return g && g.gamefaqsUserRating != null ? g.gamefaqsUserRating : null;
};
const predLength = (row) => {
  const e = ENRICH[row._k] || {};
  return e.hltbBest != null ? e.hltbBest : null;
};

/* WHEN you rated it, which turns out to matter more than almost anything else about the
   game. Your standard has tightened relentlessly: you averaged 82 in 2009 and 59 in 2025.
   A model blind to that is fitting a 17-year average of a moving target — and, worse, it
   quietly predicts on the OLD scale. Trained on games finished before 2024 and asked about
   the ones since, the previous model came out 4.2 points too generous, every time.

   So the year is a feature. For a game you HAVE rated it's the year you rated it; for one
   you haven't, it is now — because the question is what you'd make of it today, not what
   2013-you would have said. */
const NOW_YEAR = new Date().getFullYear();
const ratedYear = (row) => {
  const d = row.date || row.dateCompleted;
  const y = d ? +String(d).slice(0, 4) : NaN;
  return Number.isFinite(y) ? y : null;
};

let _model = null;
let _multiStats = null;
const resetTaste = () => { _model = null; _multiStats = null; PRED_CACHE = new WeakMap(); };

// ---- linear algebra (a library would be heavier than the maths) -------------
function solve(A, b) {
  const n = b.length;
  const M = A.map((row, i) => [...row, b[i]]);
  for (let c = 0; c < n; c++) {
    let piv = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[piv][c])) piv = r;
    if (Math.abs(M[piv][c]) < 1e-12) return null;
    [M[c], M[piv]] = [M[piv], M[c]];
    for (let r = 0; r < n; r++) {
      if (r === c) continue;
      const f = M[r][c] / M[c][c];
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
    }
  }
  // row[i] IS the pivot — `row[i][i]` indexes into a number and yields undefined,
  // which quietly turns the whole solve into NaN (it did).
  return M.map((row, i) => row[n] / row[i]);
}

// Standardise before fitting. Most features here are "your average score for games like
// this", so they all sit in a tight band around your global mean (~0.70) and are near
// collinear. Ridge on raw features of that shape is pathological: it shrinks every
// coefficient to nothing and the intercept eats the whole prediction, which is exactly what
// happened — the first version predicted ~70% for everything and lost to quoting Metacritic.
function standardise(X) {
  const d = X[0].length - 1;               // last column is the intercept
  const mu = new Array(d).fill(0), sd = new Array(d).fill(1);
  for (let j = 0; j < d; j++) {
    let m = 0;
    for (const row of X) m += row[j];
    mu[j] = m / X.length;
    let v = 0;
    for (const row of X) v += (row[j] - mu[j]) ** 2;
    sd[j] = Math.sqrt(v / X.length) || 1;  // a constant column stays constant
  }
  return (row) => row.map((v, j) => (j < d ? (v - mu[j]) / sd[j] : 1));
}

function ridgeFit(X, y, lambda) {
  const d = X[0].length;
  const A = Array.from({ length: d }, () => new Array(d).fill(0));
  const b = new Array(d).fill(0);
  for (let i = 0; i < X.length; i++) {
    for (let j = 0; j < d; j++) {
      b[j] += X[i][j] * y[i];
      for (let k = 0; k < d; k++) A[j][k] += X[i][j] * X[i][k];
    }
  }
  // Penalise the standardised features, never the intercept (the last column).
  for (let j = 0; j < d - 1; j++) A[j][j] += lambda;
  return solve(A, b) || new Array(d).fill(0);
}

const avg = (xs) => xs.reduce((a, b) => a + b, 0) / (xs.length || 1);
const med = (xs) => {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

/* An ENCODER is everything learned from a set of games: the group averages, the two
   calibrations, the length median. Built from a training set alone, then applied to games
   it has never seen. Cross-validation rebuilds one per fold; the live model builds one from
   everything you have rated. */
function fitEncoder(train) {
  const global = avg(train.map((r) => r.rating));

  const single = {};
  for (const f of PRED_FEATURES) {
    const m = new Map();
    for (const r of train) {
      const v = r[f];
      if (!v) continue;
      const e = m.get(v) || { sum: 0, n: 0 };
      e.sum += r.rating; e.n += 1;
      m.set(v, e);
    }
    single[f] = m;
  }
  const multi = {};
  for (const f of PRED_MULTI_KEYS) {
    const m = new Map();
    for (const r of train) {
      for (const raw of PRED_MULTI[f](r)) {
        const v = pnorm(raw);
        const e = m.get(v) || { sum: 0, n: 0 };
        e.sum += r.rating; e.n += 1;
        m.set(v, e);
      }
    }
    multi[f] = m;
  }

  // An outside score, regressed through your own bias: you rate ~70 where critics say ~80,
  // so handing back their number would be wrong by ten points every time.
  const calib = (get) => {
    const pairs = train.filter((r) => get(r) != null).map((r) => [get(r), r.rating]);
    if (pairs.length <= 30) return { est: () => null, n: pairs.length, slope: 0, intercept: global };
    const mx = avg(pairs.map((p) => p[0])), my = avg(pairs.map((p) => p[1]));
    let num = 0, den = 0;
    for (const [x, y] of pairs) { num += (x - mx) * (y - my); den += (x - mx) ** 2; }
    const slope = den > 1e-9 ? num / den : 0;
    const intercept = my - slope * mx;
    return {
      est: (v) => (v == null ? null : Math.max(0.15, Math.min(1, slope * v + intercept))),
      n: pairs.length, slope, intercept,
    };
  };
  const C = calib(predCritic);
  const U = calib(predPlayers);
  const lenMed = med(train.map(predLength).filter((v) => v != null));
  const midYear = med(train.map(ratedYear).filter((v) => v != null)) || NOW_YEAR;

  const shrink = (sum, n) => (n < 1 ? null : (sum + PRIOR_K * global) / (n + PRIOR_K));

  // A group average with `self` excluded — leave-one-out during training, a plain average
  // at prediction time (when the game isn't in the data anyway).
  const groupEst = (f, value, self) => {
    const e = single[f].get(value);
    if (!e) return null;
    return shrink(e.sum - (self ? self.rating : 0), e.n - (self ? 1 : 0));
  };
  // The multi-valued version: every tag the game carries votes, weighted by how many of
  // your games back it, so a well-evidenced "Metroidvania" outvotes a one-off tag.
  const multiEst = (f, row, self) => {
    const vals = PRED_MULTI[f](row);
    if (!vals.length) return null;
    let acc = 0, wsum = 0;
    for (const raw of vals) {
      const e = multi[f].get(pnorm(raw));
      if (!e) continue;
      const n = e.n - (self ? 1 : 0);
      const est = shrink(e.sum - (self ? self.rating : 0), n);
      if (est == null) continue;
      const w = Math.min(n, 50);
      acc += w * est; wsum += w;
    }
    return wsum ? acc / wsum : null;
  };

  const featurise = (row, self) => {
    const xs = [];
    for (const f of PRED_FEATURES) xs.push(groupEst(f, row[f], self) ?? global);
    for (const f of PRED_MULTI_KEYS) xs.push(multiEst(f, row, self) ?? global);

    const c = predCritic(row), u = predPlayers(row);
    const ce = C.est(c), ue = U.est(u);
    xs.push(ce ?? global);
    xs.push(c != null ? 1 : 0);            // whether we have a critic score at all
    xs.push(ue ?? global);
    xs.push(u != null ? 1 : 0);
    // An outside verdict needn't map onto yours in a straight line — you may be harsher on
    // mediocre games than you are generous with great ones. A hinge each way lets it bend.
    const cd = (ce ?? global) - global, ud = (ue ?? global) - global;
    xs.push(Math.max(0, cd)); xs.push(Math.max(0, -cd));
    xs.push(Math.max(0, ud)); xs.push(Math.max(0, -ud));
    // Where critics and players DISAGREE is exactly where either one alone misleads.
    xs.push(ce != null && ue != null ? ce - ue : 0);

    const h = predLength(row);
    xs.push(Math.log1p(h != null ? h : lenMed));        // length has a long tail, so log it
    const yr = row.releaseYear ? +row.releaseYear : null;
    xs.push(yr ? (yr - 2005) / 10 : 0);
    const cc = (ENRICH[row._k] || {}).criticCount;
    xs.push(cc != null ? Math.log1p(cc) : 0);           // how reviewed = how big a release

    // Your drifting standard. Three cases, and getting the middle one wrong quietly ruins
    // the feature: a rated game with a date sits in the year you rated it; a rated game
    // with NO date (451 of them) sits in the middle of your history, because dumping it on
    // "today" tells the model you handed out 85s this year when you didn't; and an unplayed
    // game sits TODAY, because the question is what you'd make of it now.
    const ry = ratedYear(row) ?? (row.rating != null ? midYear : NOW_YEAR);
    xs.push((ry - 2015) / 10);

    xs.push(1);                                          // intercept
    return xs;
  };

  // "Your usual" is not your seventeen-year average — it is what you'd give an ordinary
  // game NOW. The verdict ("better than your usual") is read against this, or every
  // prediction would look like a disappointment purely because you've grown harsher.
  const recent = train.filter((r) => (ratedYear(r) ?? 0) >= NOW_YEAR - 3).map((r) => r.rating);
  const baselineNow = recent.length >= 30 ? avg(recent) : global;

  return { global, baselineNow, featurise, groupEst, single, multi, critic: C, players: U };
}

// Fit the weights, using leave-one-out features so the model never sees its own answer.
function fitWeights(train, enc) {
  const X = train.map((r) => enc.featurise(r, r));
  const y = train.map((r) => r.rating);
  const scale = standardise(X);
  const w = ridgeFit(X.map(scale), y, RIDGE * X.length);
  return (xs) => scale(xs).reduce((a, v, i) => a + v * w[i], 0);
}

// ---- the model -------------------------------------------------------------
function tasteModel() {
  if (_model) return _model;
  const done = ((DATA.sheets.completed || {}).rows || []).filter((r) => r.rating != null);
  if (done.length < MIN_HISTORY) return (_model = { ok: false, n: done.length });

  // Five-fold CV. The encoder is rebuilt inside the loop on purpose: the group averages are
  // themselves learned from your ratings, so a model scored against games that shaped its
  // own features would be marking its own homework.
  const errs = [], errsMean = [], errsCritic = [];
  for (let f = 0; f < CV_FOLDS; f++) {
    const train = done.filter((_, i) => i % CV_FOLDS !== f);
    const test = done.filter((_, i) => i % CV_FOLDS === f);
    if (train.length < MIN_HISTORY || !test.length) continue;
    const enc = fitEncoder(train);
    const predict = fitWeights(train, enc);
    for (const r of test) {
      const p = Math.max(0, Math.min(1, predict(enc.featurise(r, null))));
      errs.push(Math.abs(p - r.rating));
      errsMean.push(Math.abs(enc.global - r.rating));
      const c = predCritic(r);
      errsCritic.push(Math.abs((c != null ? c : enc.global) - r.rating));
    }
  }

  // The live model: trained on everything you have rated.
  const enc = fitEncoder(done);
  const predict = fitWeights(done, enc);
  const mae = avg(errs), maeMean = avg(errsMean), maeCritic = avg(errsCritic);

  return (_model = {
    ok: true,
    global: enc.global,
    baselineNow: enc.baselineNow,
    stats: enc.single,
    multi: enc.multi,
    featurise: enc.featurise,
    groupEst: enc.groupEst,
    predict,
    critic: { slope: enc.critic.slope, intercept: enc.critic.intercept, est: enc.critic.est, n: enc.critic.n },
    players: { est: enc.players.est, n: enc.players.n },
    n: done.length,
    eval: {
      mae, maeMean, maeCritic,
      liftVsMean: 1 - mae / (maeMean || 1),
      liftVsCritic: 1 - mae / (maeCritic || 1),
      tested: errs.length,
      folds: CV_FOLDS,
    },
  });
}

// ---- prediction ------------------------------------------------------------
function predictRating(row) {
  if (!row || row.completed || row.rating != null) return null;
  const m = tasteModel();
  if (!m.ok) return null;

  // Which signals do we actually have for this game? A prediction resting on nothing but
  // the global mean isn't a prediction.
  const have = PRED_FEATURES.filter((f) => row[f] && m.stats[f].has(row[f]));
  const mc = predCritic(row);
  const pl = predPlayers(row);
  const nTags = PRED_MULTI_KEYS.reduce((a, f) => a + PRED_MULTI[f](row).length, 0);
  if (!have.length && mc == null && pl == null && !nTags) return null;

  const score = Math.max(0, Math.min(1, m.predict(m.featurise(row, null))));

  /* Structured, not a sentence: the UI renders these as bars against your baseline, so you
     can see at a glance which signals pulled the number up and which dragged it down.

     Deduped, because developer and publisher are frequently the same company and "80% on
     Blizzard" twice makes the model look like it's padding. */
  const signals = [];
  const said = new Set();
  const KIND = {
    franchise: "Series", developer: "Developer", publisher: "Publisher",
    genre: "Genre", platform: "Platform",
  };
  /* `taste` marks the signals that are things YOU have scored — the only ones that can
     honestly appear in "you rate X higher than most of what you own".

     This is a flag rather than the UI excluding kinds by name, because excluding by name
     has now broken twice: the sentence claimed "you rate Metacritic higher", and when the
     player score arrived it went straight back to claiming "you rate User score higher".
     You don't rate Metacritic; Metacritic rates the game. A blacklist has to be updated
     every time a new outside opinion is added, and it won't be. */
  for (const f of have) {
    const e = m.stats[f].get(row[f]);
    if (e.n < 2 || said.has(pnorm(row[f]))) continue;
    said.add(pnorm(row[f]));
    signals.push({ kind: KIND[f], label: String(row[f]), value: e.sum / e.n, n: e.n, taste: true });
  }
  // The IGDB tags — the genres and themes the sheet's single-word column never named, and
  // now a real part of the number, so they belong in the working too.
  for (const [f, kind] of [["igdbGenre", "Genre"], ["igdbTheme", "Theme"],
                           ["igdbPersp", "Perspective"], ["igdbKeyword", "Keyword"]]) {
    for (const raw of PRED_MULTI[f](row)) {
      const e = m.multi[f].get(pnorm(raw));
      // Keywords are fine-grained, so a handful of games behind one says nothing. Ask more
      // of them than of a genre before letting one explain a number.
      const floor = f === "igdbKeyword" ? 8 : 5;
      if (!e || e.n < floor || said.has(pnorm(raw))) continue;
      said.add(pnorm(raw));
      signals.push({ kind, label: String(raw), value: e.sum / e.n, n: e.n, taste: true });
    }
  }
  if (mc != null) {
    // Name the source that actually answered — the score may be Metacritic, IGDB's critic
    // aggregate, or the GameRankings archive, and calling all three "Metacritic" lies.
    const cs = typeof criticSourceOf === "function" ? criticSourceOf(row) : null;
    signals.push({ kind: "Critics", label: (cs && cs.label) || "Critics", value: mc, n: null, taste: false });
  }
  if (pl != null) {
    // Same courtesy: say who the players actually are.
    const e = ENRICH[row._k] || {};
    const src = e.userRating != null ? "IGDB players" : e.vnRating != null ? "VNDB" : "GameFAQs";
    signals.push({ kind: "Players", label: src, value: pl, n: null, taste: false });
  }

  const nSignals = have.length + (mc != null ? 1 : 0) + (pl != null ? 1 : 0) + (nTags ? 1 : 0);
  return {
    score,
    // Two different lines, and they are not interchangeable. The VERDICT is read against
    // what you'd give an ordinary game today; the per-signal bars are all-time group
    // averages, so they're read against your all-time average.
    baseline: m.baselineNow,
    baselineAllTime: m.global,
    confidence: Math.min(1, nSignals / 5),
    signals: signals
      .sort((a, b) => Math.abs(b.value - m.global) - Math.abs(a.value - m.global))
      .slice(0, 5),
  };
}

let PRED_CACHE = new WeakMap();
function predictedCached(row) {
  if (PRED_CACHE.has(row)) return PRED_CACHE.get(row);
  const p = predictRating(row);
  PRED_CACHE.set(row, p);
  return p;
}
const predictedOf = (row) => (predictedCached(row) || {}).score ?? null;
