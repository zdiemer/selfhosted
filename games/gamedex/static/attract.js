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
let attractStage = null;       // the stage currently on screen
let attractMuted = true;       // start muted so autoplay is never blocked

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
  attractResetProgress(true);
  $("#attract-overlay").hidden = true;
  syncScrollLock();
  if (typeof tourKick === "function") tourKick();   // let Home's ambient tour resume
}

// Clicking a game hands off to its full drawer, but attract mode isn't over — it
// just steps aside. Pause the run and hide the overlay (the drawer sits at a lower
// z-index, so it must be uncovered), silence the trailer, and let closeDrawer()
// call attractResume() to pick the slideshow back up.
function attractPause() {
  if (!attractOn || attractPaused) return;
  attractPaused = true;
  clearTimeout(attractTimer); attractTimer = null;
  attractStopPlayer(attractStage);
  $("#attract-overlay").hidden = true;
}

// Returns true if it actually resumed — closeDrawer uses that to skip its own
// scroll-unlock / tour-kick, since attract mode is taking the screen straight back.
function attractResume() {
  if (!attractOn || !attractPaused) return false;
  attractPaused = false;
  $("#attract-overlay").hidden = false;
  syncScrollLock();
  attractNext(1);                 // move on to a fresh game and restart the clock
  return true;
}

// Stop a stage's trailer (audio + loop) without tearing the whole stage down.
function attractStopPlayer(stage) {
  if (!stage) return;
  if (stage._loop) { clearInterval(stage._loop); stage._loop = null; }
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

function attractNext(dir) {
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

  attractResetProgress();
  attractTimer = setTimeout(() => attractNext(1), ATTRACT_DWELL);
}

// The countdown bar: fill 0→100% over one dwell, restarted on every game. It's
// the "when does it move on" indicator; a manual next/prev just restarts it.
function attractResetProgress(clear) {
  const fill = $("#attractProgressFill");
  if (!fill) return;
  fill.style.transition = "none";
  fill.style.width = "0%";
  if (clear) return;
  void fill.offsetWidth;                                   // commit the reset before animating
  fill.style.transition = `width ${ATTRACT_DWELL}ms linear`;
  fill.style.width = "100%";
}

function attractBuildStage(entry) {
  const { row, sheetKey } = entry;
  const k = row._k;
  const e = ENRICH[k] || {};
  const cs = coverSrc(e, "cover_big");

  const stage = document.createElement("div");
  stage.className = "attract-stage";
  stage._loop = null; stage._watch = null; stage._frame = null; stage._dead = false;

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

  // Click anywhere on a game → step aside into its full drawer; closing the drawer
  // resumes the slideshow (attractPause + closeDrawer's attractResume hook).
  stage.addEventListener("click", () => { attractPause(); openDrawer(row, sheetKey); });

  const vid = e.video;
  const wantsVideo = vid && !YT_BLOCKED && WANTS_MOTION;
  if (wantsVideo) attractPlayVideo(stage, bg, vid, cs);
  else attractShowArt(bg, cs, false);

  // Upgrade with the full detail once it lands: summary, genres, rating, and — when
  // there's no trailer — a screenshot backdrop, which beats the box art.
  if (k) attractFetchDetail(k).then((d) => {
    if (!d || stage._dead) return;
    attractFillDetail(stage, row, d);
    if (!wantsVideo) {
      const shot = (d.screenshots || [])[0] || (d.artworks || [])[0];
      if (shot) attractShowArt(bg, IMG(shot, "screenshot_big"), true);
    }
  });

  return stage;
}

// A still backdrop: blurred and dimmed so the details read over it, optionally with
// a slow Ken-Burns drift (only when the game has no trailer and motion is welcome).
function attractShowArt(bg, src, motion) {
  if (!src) { bg.innerHTML = ""; bg.classList.add("attract-bg-empty"); return; }
  bg.classList.remove("attract-bg-empty");
  bg.innerHTML = `<img class="attract-art${motion && WANTS_MOTION ? " kb" : ""}" src="${escapeHtml(src)}" alt="">`;
}

// The trailer as a full-bleed backdrop. Same clean-embed trick as the hover
// preview (no start/loop params — seek + loop over the IFrame API instead, so
// YouTube doesn't draw its full chrome), sized to cover the viewport. It fades in
// only once it truly starts, so a dead/blocked trailer just leaves the art showing.
function attractPlayVideo(stage, bg, vid, cs) {
  attractShowArt(bg, cs, false);          // art shows first; video reveals over it
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
  bg.appendChild(wrap);
  stage._frame = frame;

  let duration = 0, clip = null;
  stage._watch = ytWatch(frame,
    () => { wrap.remove(); stage._frame = null; },     // never played → keep the art
    () => { wrap.classList.add("on"); ytCmd(frame, attractMuted ? "mute" : "unMute"); },
    (info) => {
      if (info.duration) duration = info.duration;
      if (!clip && duration) {
        clip = previewClip(vid, duration);
        ytCmd(frame, "seekTo", [clip.start, true]);
        stage._loop = setInterval(() => ytCmd(frame, "seekTo", [clip.start, true]), clip.len * 1000);
      }
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
})();
