"use strict";

// ---- Attract mode --------------------------------------------------------
// A lean-back, full-screen slideshow: cross-fade between random games, each
// shown as full-screen details with its trailer autoplaying (muted) behind.
// A game with no trailer falls back to a blurred, slowly-panning screenshot.
//
// This rides entirely on machinery app.js already has: the YouTube engine
// (ytSrc / ytWatch / ytCmd / previewClip, bot-wall handling and all), the
// enrichment caches (ENRICH light + DETAIL full), and the overlay conventions
// (anyOverlayOpen + syncScrollLock). Registering #attract-overlay in
// anyOverlayOpen() is what makes the ambient card tour stand down while this is
// up. Launched from a button on Home; Esc / × exits back to it.

const ATTRACT_DWELL = 16000;   // how long each game holds the screen
const ATTRACT_FADE  = 900;     // cross-fade duration — MUST match the CSS transition

let attractOn = false;
let attractPaused = false;      // true while a game's drawer is open over a paused run
let attractPool = [];          // shuffled [{row, sheetKey}]
let attractIdx = -1;
let attractTimer = null;
let attractDwellStart = 0;     // Date.now() when the current game's countdown began
let attractDwellLeft = 16000;  // ms budgeted for it — trimmed to what's LEFT on a resume
let attractStartPct = 0;       // bar % the current countdown began at (nonzero after a resume)
let attractStage = null;       // the stage currently on screen
let attractMuted = true;       // start muted so autoplay is never blocked
let attractWentFullscreen = false;   // we requested fullscreen and should undo it on exit
let attractIdleTimer = null;         // desktop: hide the chrome after a spell of no movement
const ATTRACT_IDLE = 2600;

// Fullscreen + auto-hiding controls are desktop affordances: touch has no mouse to
// go idle, and mobile fullscreen is a mess. Gate both on a fine pointer.
const attractDesktop = () => window.matchMedia("(pointer: fine)").matches;

// Fisher–Yates. Home's shuffle is date-seeded (stable per day) on purpose; an
// attract run wants fresh randomness every time, so it gets its own.
function attractShuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function attractBuildPool() {
  const out = [];
  const add = (sheetKey) => {
    for (const row of ((DATA.sheets[sheetKey] || {}).rows || [])) {
      // A key we matched to IGDB (or at least haven't ruled out) — anything else
      // has no cover, no video and no detail, so it's a blank stage.
      if (row && row._k && !NO_MATCH.has(row._k)) out.push({ row, sheetKey });
    }
  };
  TABS.forEach(add);
  return attractShuffle(out);
}

function attractTitle(entry) {
  const cols = (DATA.sheets[entry.sheetKey] || DATA.sheets.games).columns;
  const key = cols && cols[0] ? cols[0].key : "title";
  return String(entry.row[key] ?? entry.row.title ?? "Untitled");
}

function openAttract() {
  if (attractOn || !DATA) return;
  attractPool = attractBuildPool();
  if (!attractPool.length) return;
  attractOn = true;
  attractPaused = false;
  attractIdx = -1;
  // Nothing should be playing behind the overlay: stop the tour AND any hover.
  if (typeof tourStop === "function") tourStop();
  if (typeof stopPreview === "function") stopPreview();
  $("#attractStages").innerHTML = "";
  attractStage = null;
  $("#attract-overlay").hidden = false;
  document.addEventListener("keydown", attractKey, true);
  syncScrollLock();
  attractApplyMuteBtn();
  if (attractDesktop()) {
    attractRequestFullscreen();                          // needs the launch click's gesture
    document.addEventListener("mousemove", attractPoke, true);
    attractPoke();                                       // arm the idle-hide countdown
  }
  attractNext(1);
}

function closeAttract() {
  if (!attractOn) return;
  attractOn = false;
  attractPaused = false;
  clearTimeout(attractTimer); attractTimer = null;
  document.removeEventListener("keydown", attractKey, true);
  const host = $("#attractStages");
  if (host) {
    host.querySelectorAll(".attract-stage").forEach(attractTeardownStage);
    host.innerHTML = "";
  }
  attractStage = null;
  attractClearProgress();
  document.removeEventListener("mousemove", attractPoke, true);
  clearTimeout(attractIdleTimer); attractIdleTimer = null;
  $("#attract-overlay").classList.remove("attract-idle", "attract-behind");
  attractExitFullscreen();
  $("#attract-overlay").hidden = true;
  syncScrollLock();
  if (typeof tourKick === "function") tourKick();   // let Home's ambient tour resume
}

function attractRequestFullscreen() {
  const el = document.documentElement;                  // the PAGE, not the overlay: hiding
  if (document.fullscreenElement || !el.requestFullscreen) return;  // the overlay when a
  el.requestFullscreen().then(() => { attractWentFullscreen = true; }).catch(() => {});
}                                                        // drawer opens must not drop fullscreen
function attractExitFullscreen() {
  if (attractWentFullscreen && document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
  attractWentFullscreen = false;
}

// Desktop: reveal the controls/hint on any movement, then fade them (and the cursor)
// after a still moment. Never hides while a drawer is open over a paused run.
function attractPoke() {
  const ov = $("#attract-overlay");
  if (!ov) return;
  ov.classList.remove("attract-idle");
  clearTimeout(attractIdleTimer);
  attractIdleTimer = setTimeout(() => {
    if (attractOn && !attractPaused) ov.classList.add("attract-idle");
  }, ATTRACT_IDLE);
}

// Clicking a game hands off to its full drawer, but attract mode isn't over — it
// just steps aside. Freeze the run (stop the trailer + countdown) and drop the overlay
// BELOW the drawer rather than hiding it, so the game's scene still shows through the
// drawer's scrim. closeDrawer() calls attractResume() to pick it back up.
function attractPause() {
  if (!attractOn || attractPaused) return;
  attractPaused = true;
  // Freeze the countdown mid-flight (all from time, no DOM reads): how far the bar had
  // filled, and how much of this game's turn is left to bank for the resume.
  const elapsed = Date.now() - attractDwellStart;
  const frac = attractDwellLeft > 0 ? Math.min(1, elapsed / attractDwellLeft) : 1;
  attractStartPct = attractStartPct + (100 - attractStartPct) * frac;
  attractDwellLeft = Math.max(1500, attractDwellLeft - elapsed);
  clearTimeout(attractTimer); attractTimer = null;
  clearTimeout(attractIdleTimer); attractIdleTimer = null;
  attractSetProgressStatic(attractStartPct);
  attractStopPlayer(attractStage);
  const ov = $("#attract-overlay");
  ov.classList.remove("attract-idle");
  ov.classList.add("attract-behind");
}

// Returns true if it actually resumed — closeDrawer uses that to skip its own
// scroll-unlock / tour-kick, since attract mode is taking the screen straight back.
function attractResume() {
  if (!attractOn || !attractPaused) return false;
  attractPaused = false;
  $("#attract-overlay").classList.remove("attract-behind");
  syncScrollLock();
  if (attractDesktop()) attractPoke();
  attractNext(0, attractDwellLeft, attractStartPct);   // same game, from where its turn paused
  return true;
}

// Stop a stage's trailer (audio + loop) without tearing the whole stage down.
function attractStopPlayer(stage) {
  if (!stage) return;
  if (stage._loop) { clearInterval(stage._loop); stage._loop = null; }
  if (stage._shotLoop) { clearInterval(stage._shotLoop); stage._shotLoop = null; }
  if (stage._watch) { stage._watch(); stage._watch = null; }
  if (stage._frame) {
    const wrap = stage._frame.closest(".attract-video");
    (wrap || stage._frame).remove();
    stage._frame = null;
  }
}

// Attract owns the keyboard while it's up, so global shortcuts don't fire behind
// the overlay (capture + stopPropagation beats app.js's document-level listeners).
function attractKey(e) {
  // Paused (a game's drawer is open over us) → the drawer owns the keyboard.
  if (!attractOn || attractPaused || $("#attract-overlay").hidden) return;
  e.stopPropagation();
  switch (e.key) {
    case "Escape":     e.preventDefault(); closeAttract(); break;
    case "ArrowRight": e.preventDefault(); attractNext(1); break;
    case "ArrowLeft":  e.preventDefault(); attractNext(-1); break;
    case "m": case "M": e.preventDefault(); attractToggleMute(); break;
  }
}

function attractNext(dir, dwellMs, startPct) {
  if (!attractOn) return;
  clearTimeout(attractTimer); attractTimer = null;
  const n = attractPool.length;
  attractIdx = ((attractIdx + dir) % n + n) % n;
  const entry = attractPool[attractIdx];

  const host = $("#attractStages");
  // Rapid arrow-mashing can outrun the fade; keep at most the outgoing stage.
  host.querySelectorAll(".attract-stage").forEach((s) => { if (s !== attractStage) attractTeardownStage(s); });
  const prev = attractStage;

  const stage = attractBuildStage(entry);
  host.appendChild(stage);
  void stage.offsetWidth;                 // commit initial opacity:0 before fading in
  stage.classList.add("attract-in");
  if (prev) {
    prev.classList.remove("attract-in");
    setTimeout(() => attractTeardownStage(prev), ATTRACT_FADE);
  }
  attractStage = stage;

  // Warm the next game's detail so its summary/screenshot is ready at swap time.
  const nxt = attractPool[(attractIdx + 1) % n];
  if (nxt && nxt.row._k) attractFetchDetail(nxt.row._k);

  // A fresh game (or a manual next/prev) gets the full dwell from an empty bar. A
  // resume passes the time that was LEFT when the drawer opened AND the bar % it froze
  // at, so the countdown continues from there rather than restarting.
  const ms = dwellMs || ATTRACT_DWELL;
  attractDwellStart = Date.now();
  attractDwellLeft = ms;
  attractStartPct = startPct || 0;
  attractSetProgress(attractStartPct, ms);
  attractTimer = setTimeout(() => attractNext(1), ms);
}

// The countdown bar. Progress is tracked from TIME (not by measuring the animating
// DOM), so freezing it mid-flight is exact. attractSetProgress animates startPct→100%
// over `ms`; attractSetProgressStatic pins it; attractClearProgress empties it.
function attractSetProgress(startPct, ms) {
  const fill = $("#attractProgressFill");
  if (!fill) return;
  fill.style.transition = "none";
  fill.style.width = startPct + "%";
  void fill.offsetWidth;                                   // commit the start before animating
  fill.style.transition = `width ${ms}ms linear`;
  fill.style.width = "100%";
}
function attractSetProgressStatic(pct) {
  const fill = $("#attractProgressFill");
  if (!fill) return;
  fill.style.transition = "none";
  fill.style.width = pct + "%";
}
function attractClearProgress() { attractSetProgressStatic(0); }

function attractBuildStage(entry) {
  const { row, sheetKey } = entry;
  const k = row._k;
  const e = ENRICH[k] || {};
  const cs = coverSrc(e, "cover_big");

  const stage = document.createElement("div");
  stage.className = "attract-stage";
  stage._loop = null; stage._watch = null; stage._frame = null; stage._shotLoop = null;
  stage._dead = false; stage._videoLive = false; stage._detail = null; stage._k = k;

  const bg = document.createElement("div");
  bg.className = "attract-bg";
  const scrim = document.createElement("div");
  scrim.className = "attract-scrim";
  const info = document.createElement("div");
  info.className = "attract-info";

  const bits = [row.platform, row.releaseYear || row.releaseDate || row.release, row.genre]
    .filter((x) => x != null && x !== "")
    .map((x) => `<span>${escapeHtml(String(x))}</span>`).join("");
  info.innerHTML =
    (cs ? `<img class="attract-cover" src="${escapeHtml(cs)}" alt="">` : "") +
    `<div class="attract-txt">
       <h1>${escapeHtml(attractTitle(entry))}</h1>
       <div class="attract-meta">${bits}</div>
       <div class="attract-genres"></div>
       <p class="attract-summary"></p>
     </div>`;

  stage.appendChild(bg);
  stage.appendChild(scrim);
  stage.appendChild(info);
  stage._bg = bg; stage._scrim = scrim;

  // Click anywhere on a game → step aside into its full drawer; closing the drawer
  // resumes the slideshow (attractPause + closeDrawer's attractResume hook).
  stage.addEventListener("click", () => { attractPause(); openDrawer(row, sheetKey); });

  // The BACKDROP is always the game's own imagery — the cover right away, upgraded to
  // a cross-fading screenshot slideshow once the detail lands. A trailer, when we have
  // one, is a separate layer that fades in OVER this only if it genuinely plays. So a
  // device that blocks YouTube (or a dead trailer) just shows the screenshots — no
  // stretched box art, no black rectangle.
  attractShowArt(bg, cs, true);
  if (k) attractFetchDetail(k).then((d) => {
    if (!d || stage._dead) return;
    stage._detail = d;
    attractFillDetail(stage, row, d);
    if (!stage._videoLive) attractShowBackdrop(stage, d, cs);
  });

  if (e.video && !YT_BLOCKED && WANTS_MOTION) attractPlayVideo(stage, e.video, cs);

  return stage;
}

// Pick the best backdrop from a game's detail: cross-fade its screenshots (then
// artworks); a single image gets a Ken-Burns still; nothing usable falls back to the
// cover, which at least pans rather than sitting there stretched.
function attractShowBackdrop(stage, d, cs) {
  const shots = [...((d && d.screenshots) || []), ...((d && d.artworks) || [])]
    .slice(0, 6).map((id) => IMG(id, "screenshot_big"));
  if (WANTS_MOTION && shots.length > 1) attractRunShots(stage, stage._bg, shots);
  else attractShowArt(stage._bg, shots[0] || cs, true);
}

// A slow Ken-Burns pan+zoom, each image in a RANDOM direction so consecutive shots
// don't all drift the same way. The end point and duration ride on CSS vars.
function attractArtImg(src, motion) {
  const img = document.createElement("img");
  img.className = "attract-art" + (motion && WANTS_MOTION ? " kb" : "");
  img.src = src;
  if (motion && WANTS_MOTION) {
    img.style.setProperty("--kb-x", (Math.random() * 10 - 5).toFixed(1) + "%");
    img.style.setProperty("--kb-y", (Math.random() * 10 - 5).toFixed(1) + "%");
    img.style.setProperty("--kb-s", (1.16 + Math.random() * 0.12).toFixed(3));
    img.style.setProperty("--kb-dur", (16 + Math.random() * 8).toFixed(1) + "s");
  }
  return img;
}

// A single blurred, dimmed backdrop image (optionally panning).
function attractShowArt(bg, src, motion) {
  if (!src) { bg.innerHTML = ""; bg.classList.add("attract-bg-empty"); return; }
  bg.classList.remove("attract-bg-empty");
  bg.innerHTML = "";
  bg.appendChild(attractArtImg(src, motion));
}

const ATTRACT_SHOT_MS = 4200;   // how long each screenshot holds before the next fades in
// Cross-fade through several screenshots for the length of the game's turn. New layers
// fade in over the old (which is dropped after the fade), so at most two are ever live.
function attractRunShots(stage, bg, shots) {
  if (stage._shotLoop) { clearInterval(stage._shotLoop); stage._shotLoop = null; }
  bg.classList.remove("attract-bg-empty");
  bg.innerHTML = "";
  let i = 0, cur = null;
  const show = (idx) => {
    const img = attractArtImg(shots[idx], true);
    img.style.opacity = "0";
    bg.appendChild(img);
    void img.offsetWidth;                     // commit opacity:0 so the fade-in runs
    img.style.opacity = "";                   // → stylesheet's 1, transitions in
    const old = cur; cur = img;
    if (old) setTimeout(() => old.remove(), 950);
  };
  show(0);
  stage._shotLoop = setInterval(() => { i = (i + 1) % shots.length; show(i); }, ATTRACT_SHOT_MS);
}

// The trailer as a full-bleed layer OVER the screenshot backdrop. Same clean-embed
// trick as the hover preview (no start/loop params — seek + loop over the IFrame API,
// so YouTube draws no chrome). It stays invisible until it truly starts, so a blocked
// or dead trailer never shows — the screenshots behind it carry the screen. If it does
// start, it stops the slideshow (no point cross-fading behind an opaque video).
function attractPlayVideo(stage, vid, cs) {
  const wrap = document.createElement("div");
  wrap.className = "attract-video";
  const frame = document.createElement("iframe");
  frame.className = "attract-frame";
  frame.src = ytSrc(vid, {
    autoplay: "1", mute: "1", controls: "0", disablekb: "1", iv_load_policy: "3", fs: "0",
  });
  frame.allow = "autoplay; encrypted-media";
  frame.tabIndex = -1;
  frame.setAttribute("aria-hidden", "true");
  wrap.appendChild(frame);
  stage.insertBefore(wrap, stage._scrim);   // above the backdrop, below scrim + details
  stage._frame = frame;

  let duration = 0, clip = null;
  stage._watch = ytWatch(frame,
    () => { wrap.remove(); stage._frame = null; attractVideoFailed(stage, cs); },
    () => {
      stage._videoLive = true;
      if (stage._shotLoop) { clearInterval(stage._shotLoop); stage._shotLoop = null; }
      wrap.classList.add("on");
      ytCmd(frame, attractMuted ? "mute" : "unMute");
    },
    (info) => {
      if (info.duration) duration = info.duration;
      if (!clip && duration) {
        clip = previewClip(vid, duration);
        ytCmd(frame, "seekTo", [clip.start, true]);
        stage._loop = setInterval(() => ytCmd(frame, "seekTo", [clip.start, true]), clip.len * 1000);
      }
    });
}

// The trailer never played — make sure the screenshot backdrop is up (it usually
// already is from the detail fetch; this covers the race where detail lands late).
function attractVideoFailed(stage, cs) {
  if (stage._dead || stage._videoLive || stage._shotLoop) return;
  if (stage._detail) attractShowBackdrop(stage, stage._detail, cs);
  else if (stage._k) attractFetchDetail(stage._k).then((d) => {
    if (d && !stage._dead && !stage._videoLive) { stage._detail = d; attractShowBackdrop(stage, d, cs); }
  });
}

function attractFillDetail(stage, row, d) {
  const g = stage.querySelector(".attract-genres");
  if (g) {
    const curated = (typeof curatedGenres === "function") ? curatedGenres(row) : [];
    const genres = [...new Set([...curated, ...(d.genres || []).map((x) => String(canonGenre(x)))])].slice(0, 4);
    g.innerHTML = genres.map((x) => `<span class="attract-genre">${escapeHtml(x)}</span>`).join("");
  }
  const s = stage.querySelector(".attract-summary");
  if (s) s.textContent = d.summary || d.storyline || "";   // textContent → no escaping needed
  if (d.rating != null) {
    const meta = stage.querySelector(".attract-meta");
    if (meta && !meta.querySelector(".attract-rating")) {
      const b = document.createElement("span");
      b.className = "attract-rating " + ratingClass(d.rating);
      b.textContent = Math.round(d.rating * 100) + " / 100";
      meta.appendChild(b);
    }
  }
}

function attractTeardownStage(stage) {
  if (!stage) return;
  stage._dead = true;
  if (stage._loop) { clearInterval(stage._loop); stage._loop = null; }
  if (stage._shotLoop) { clearInterval(stage._shotLoop); stage._shotLoop = null; }
  if (stage._watch) { stage._watch(); stage._watch = null; }   // ytWatch's own cleanup
  stage.remove();
}

// Detail fetch that only cares about the IGDB record (loadDetail does much more,
// for the drawer). Shares the DETAIL cache, de-dupes in-flight requests.
const _attractDetailReq = {};
function attractFetchDetail(k) {
  if (DETAIL[k]) return Promise.resolve(DETAIL[k]);
  if (_attractDetailReq[k]) return _attractDetailReq[k];
  const p = fetch("api/enrichment/detail?key=" + encodeURIComponent(k))
    .then((r) => r.json())
    .then((j) => (j && j.status === "matched" && j.detail) ? (DETAIL[k] = j.detail) : null)
    .catch(() => null)
    .finally(() => { delete _attractDetailReq[k]; });
  _attractDetailReq[k] = p;
  return p;
}

function attractToggleMute() {
  attractMuted = !attractMuted;
  if (attractStage && attractStage._frame) ytCmd(attractStage._frame, attractMuted ? "mute" : "unMute");
  attractApplyMuteBtn();
}
function attractApplyMuteBtn() {
  const b = $("#attractMute");
  if (!b) return;
  b.innerHTML = icon(attractMuted ? "i-muted" : "i-volume", 20);
  b.setAttribute("aria-label", attractMuted ? "Unmute" : "Mute");
}

// Controls live in index.html (fixtures of the overlay), so wire them once at load.
// stopPropagation keeps a control click from also triggering the stage's open-drawer.
(function attractWireControls() {
  const bind = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
  };
  bind("attractClose", closeAttract);
  bind("attractMute", attractToggleMute);
  bind("attractPrev", () => attractNext(-1));
  bind("attractNext", () => attractNext(1));

  // Leaving fullscreen (Esc / F11) while watching means "I'm done" — exit attract too,
  // so a single Esc gets you all the way out. Skipped while a game's drawer is open over
  // a paused run (that Esc belongs to the drawer), and a no-op when WE dropped fullscreen
  // on the way out (attractWentFullscreen is already cleared by then).
  document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement) return;
    const wasOurs = attractWentFullscreen;
    attractWentFullscreen = false;
    if (wasOurs && attractOn && !attractPaused) closeAttract();
  });
})();
