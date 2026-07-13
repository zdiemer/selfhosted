"use strict";

/* Predicted rating — what you'd probably score a game you haven't played.

   A small ridge regression, trained in the browser on your own rated
   completions. No library and no server: 1,707 rows and six features is a 6x6
   matrix solve, which is microseconds.

   FEATURES (each is "your average score for games sharing this attribute",
   shrunk toward your overall mean — see below):

       franchise · developer · publisher · genre · platform · what critics said

   Three things stop this being self-congratulatory nonsense:

   1. SHRINKAGE. "You rated the one Bloodborne you played 100%" is not evidence
      that you'd rate every FromSoftware game 100%. Each group average is pulled
      toward your global mean in proportion to how little data backs it:

          estimate = (n * groupMean + k * globalMean) / (n + k)

      One data point barely moves off your baseline; thirty are trusted nearly
      fully.

   2. LEAVE-ONE-OUT, so it can't cheat. When building the training row for a game
      you finished, that game is REMOVED from its own group averages. Without
      this the model sees its own answer in its own features — "your average
      Castlevania score" would already contain the Castlevania we're asking about
      — and it would look brilliant and predict nothing.

   3. LEARNED WEIGHTS, not guessed ones. The blend is fitted by ridge regression
      rather than hand-tuned, and scored on a held-out third of your games
      against two baselines: predicting your mean every time, and just handing
      back the Metacritic score. If it can't beat those, it says so.

   Loaded after app.js; shares its globals. */

const PRIOR_K = 6;               // evidence needed before a group average is trusted
const RIDGE = 0.02;              // regularisation (on standardised features)
const MIN_HISTORY = 60;          // below this we simply don't have enough to say

const PRED_FEATURES = ["franchise", "developer", "publisher", "genre", "platform"];

let _model = null;
const resetTaste = () => { _model = null; PRED_CACHE = new WeakMap(); };

// ---- linear algebra (6x6; a library would be heavier than the maths) --------
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

// Standardise before fitting. Every feature here is "your average score for
// games like this", so they all sit in a tight band around your global mean
// (~0.70) and are near-collinear. Ridge on raw features of that shape is
// pathological: it shrinks every coefficient to nothing and the intercept eats
// the whole prediction, which is exactly what happened — the first version
// predicted ~70% for everything and lost to simply quoting Metacritic.
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
  const apply = (row) => row.map((v, j) => (j < d ? (v - mu[j]) / sd[j] : 1));
  return { apply, mu, sd };
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

// ---- the model -------------------------------------------------------------
function tasteModel() {
  if (_model) return _model;
  const done = ((DATA.sheets.completed || {}).rows || []).filter((r) => r.rating != null);
  if (done.length < MIN_HISTORY) return (_model = { ok: false, n: done.length });

  const mean = (xs) => xs.reduce((a, b) => a + b, 0) / (xs.length || 1);
  const global = mean(done.map((r) => r.rating));

  // Sums per attribute value, so a leave-one-out average is just a subtraction.
  const stats = {};
  for (const f of PRED_FEATURES) {
    const m = new Map();
    for (const r of done) {
      const v = r[f];
      if (!v) continue;
      const e = m.get(v) || { sum: 0, n: 0 };
      e.sum += r.rating; e.n += 1;
      m.set(v, e);
    }
    stats[f] = m;
  }

  // The critic signal, regressed through your own bias: you rate ~70 where they
  // say ~80, so handing back their number would be wrong by ten points every time.
  const pairs = done.filter((r) => r.criticScore != null).map((r) => [r.criticScore, r.rating]);
  let cSlope = 0, cIntercept = global;
  if (pairs.length > 30) {
    const mx = mean(pairs.map((p) => p[0])), my = mean(pairs.map((p) => p[1]));
    let num = 0, den = 0;
    for (const [x, y] of pairs) { num += (x - mx) * (y - my); den += (x - mx) ** 2; }
    if (den > 1e-9) { cSlope = num / den; cIntercept = my - cSlope * mx; }
  }
  const criticEst = (mc) => (mc == null ? null
    : Math.max(0.2, Math.min(1, cSlope * mc + cIntercept)));

  // A group average with `self` excluded — leave-one-out during training, and a
  // plain average at prediction time (when the game isn't in the data anyway).
  const groupEst = (f, value, self) => {
    const e = stats[f].get(value);
    if (!e) return null;
    const n = e.n - (self ? 1 : 0);
    const sum = e.sum - (self ? self.rating : 0);
    if (n < 1) return null;
    return (n * (sum / n) + PRIOR_K * global) / (n + PRIOR_K);
  };

  // Build a feature row. Missing signals fall back to the global mean, which is
  // the honest "I have no information" answer.
  const featurise = (row, self) => {
    const xs = PRED_FEATURES.map((f) => groupEst(f, row[f], self) ?? global);
    const c = criticEst(self ? self.criticScore : metacriticOf(row));
    xs.push(c ?? global);
    xs.push(1);                                  // intercept
    return xs;
  };

  // Train on two thirds, score on the last third. Interleaved (every third game)
  // rather than a date cut, so a change of taste over time doesn't skew it.
  const X = [], y = [], Xt = [], yt = [], mcT = [];
  done.forEach((r, i) => {
    const row = featurise(r, r);                 // leave-one-out
    if (i % 3 === 2) { Xt.push(row); yt.push(r.rating); mcT.push(r.criticScore); }
    else { X.push(row); y.push(r.rating); }
  });

  const scaler = standardise(X);
  const w = ridgeFit(X.map(scaler.apply), y, RIDGE * X.length);
  const predict = (xs) => scaler.apply(xs).reduce((a, v, i) => a + v * w[i], 0);

  // Honest scoring against the two baselines worth beating.
  const mae = (pred, act) => mean(pred.map((p, i) => Math.abs(p - act[i])));
  const ours = mae(Xt.map(predict), yt);
  const baseMean = mae(yt.map(() => global), yt);
  const withCritic = mcT.map((m, i) => (m == null ? global : m));
  const baseCritic = mae(withCritic, yt);

  return (_model = {
    ok: true, w, global, stats, featurise, predict,
    critic: { slope: cSlope, intercept: cIntercept, est: criticEst, n: pairs.length },
    groupEst,
    n: done.length,
    eval: {
      mae: ours, maeMean: baseMean, maeCritic: baseCritic,
      liftVsMean: 1 - ours / baseMean,
      liftVsCritic: 1 - ours / baseCritic,
      tested: yt.length,
    },
  });
}

// ---- prediction ------------------------------------------------------------
function predictRating(row) {
  if (!row || row.completed || row.rating != null) return null;
  const m = tasteModel();
  if (!m.ok) return null;

  // Which signals do we actually have for this game? A prediction resting on
  // nothing but the global mean isn't a prediction.
  const have = PRED_FEATURES.filter((f) => row[f] && m.stats[f].has(row[f]));
  const mc = metacriticOf(row);
  if (!have.length && mc == null) return null;

  const score = Math.max(0, Math.min(1, m.predict(m.featurise(row, null))));

  // Structured, not a sentence: the UI renders these as bars against your
  // baseline, so you can see at a glance which signals pulled the number up and
  // which dragged it down.
  //
  // Deduped, because developer and publisher are frequently the same company and
  // "80% on Blizzard" twice makes the model look like it's padding.
  const signals = [];
  const said = new Set();
  const KIND = {
    franchise: "Series", developer: "Developer", publisher: "Publisher",
    genre: "Genre", platform: "Platform",
  };
  for (const f of have) {
    const e = m.stats[f].get(row[f]);
    if (e.n < 2 || said.has(row[f])) continue;
    said.add(row[f]);
    signals.push({ kind: KIND[f], label: String(row[f]), value: e.sum / e.n, n: e.n });
  }
  if (mc != null) {
    // Name the source that actually answered — the score may be Metacritic, IGDB's critic
    // aggregate, or the GameRankings archive, and calling all three "Metacritic" lies.
    const cs = typeof criticSourceOf === "function" ? criticSourceOf(row) : null;
    signals.push({ kind: "Critics", label: (cs && cs.label) || "Critics", value: mc, n: null });
  }

  return {
    score,
    baseline: m.global,          // your average score — the line everything is read against
    confidence: Math.min(1, (have.length + (mc != null ? 1 : 0)) / 4),
    signals: signals.sort((a, b) => Math.abs(b.value - m.global) - Math.abs(a.value - m.global)).slice(0, 4),
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
