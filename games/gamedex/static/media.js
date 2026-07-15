"use strict";

/* What was actually IN the box.

   The shelf has always shown you the outside of a game. This opens it: the case swings, and the
   thing you'd actually hold comes out — a cartridge with its label, or a disc with its printed
   face — with the instruction booklet behind it.

   THE SHELLS ARE THE POINT. A grey slab with a picture on it isn't a cartridge, it's a coaster.
   Each machine's cart is a specific object and people know the difference at a glance: the NES's
   deep front bezel with the label set well back, the SNES's tapered shoulders and ridged grip,
   the N64's tall shell with finger grooves, the Game Boy's notched corner (the cut that stops you
   inserting it backwards), the GBA's stubby body that's nearly all label, the flat little cards of
   the DS and the Switch. Those are drawn as geometry, per platform, from the tables below.

   THE ART COMES FROM THREE PLACES, in this order:
     1. a real disc scan from GameTDB (`discArt`) — the genuine printed face, Nintendo optical only
     2. a real cart/disc scan — reserved for ScreenScraper, which needs a dev key we don't have yet;
        the field is read here already so that when it lands nothing else has to change
     3. DERIVED from the box art — crop the cover onto the label. Which is not a cheat: a real cart
        label usually IS a crop of the cover, so this looks right far more often than it has any
        business to, and it means no game on the shelf is ever a blank shell.

   Loaded after shelf.js; shares its globals. */

// ---- which object a platform actually is -----------------------------------
// shell: the plastic. label: where the sticker sits, as % of the face. notch: the anti-insert cut.
/* Every shell below is set from a REAL measurement, not from memory, because the first pass was
   cartridge-shaped without being any particular cartridge — and the aspect ratios especially were
   just wrong. What the references actually say:

     NES     133 x 120 x 20 mm — TALLER than wide, with a pull indent cut into the TOP-LEFT.
                                 (I had it wider than tall with a bezel all the way round.)
     SNES    120 x 110 x 20 mm — WIDER than tall, "gray rounded front", grips are ribs running
                                 down each LONG EDGE, label in a recessed well that wraps over
                                 the top. (I had ribs along the bottom, which is a Genesis idea.)
     N64     116 x  75 x 18 mm — much NARROWER and taller than I drew it; black.
     GB      65.5 x 57 x 7.5mm — taller than wide, notch in the TOP-RIGHT corner.
     GBA                       — WIDER than tall, and its notch is on a bottom REAR corner (the
                                 shape detector that tells the machine which mode to boot), so
                                 the front face has NO notch at all. I had drawn one.

   Sizes below preserve each cart's true aspect ratio; the absolute px are normalised so they all
   read at about the same size on screen, since you only ever see one at a time. */
const MEDIA = {
  "NES":                 { kind: "cart", w: 115, h: 128, d: 20, shell: "#b8b3a7", label: [12, 14, 76, 56], shape: "nes" , sprite: "nes" },
  "Nintendo Entertainment System": { kind: "cart", w: 115, h: 128, d: 20, shell: "#b8b3a7", label: [12, 14, 76, 56], shape: "nes" , sprite: "nes" },
  /* SNES (North America). Checked against a reference rather than drawn from memory, because
     the first pass was cartridge-SHAPED and not a SNES cartridge:
       - it is WIDER THAN TALL (~12cm x 11cm). I had it near-square.
       - the anti-slip grips are molded ridges running down each LONG EDGE — vertical ribbing
         on the left and right flanks. I had a horizontal ribbed strip along the bottom, which
         is a Genesis/N64 idea and not this.
       - the label sits in a RECESSED WELL with a big corner radius, and wraps over the top
         edge, so the top of the cart carries a strip of the same label.
       - the front is the famous "gray rounded front": softened top corners, not the hard
         taper I gave it. */
  "SNES":                { kind: "cart", w: 132, h: 121, d: 20, shell: "#b9b5ad", label: [17, 16, 66, 54], shape: "snes" , sprite: "snes" },
  "Super Nintendo":      { kind: "cart", w: 132, h: 121, d: 20, shell: "#b9b5ad", label: [17, 16, 66, 54], shape: "snes" , sprite: "snes" },
  "Nintendo 64":         { kind: "cart", w: 92,  h: 135, d: 20, shell: "#33353b", label: [11, 11, 78, 52], shape: "n64" , sprite: "n64" },
  "Game Boy":            { kind: "cart", w: 105, h: 121, d: 14, shell: "#c8c5bd", label: [11, 13, 78, 58], shape: "gb" , sprite: "gb" },
  "Game Boy Color":      { kind: "cart", w: 105, h: 121, d: 14, shell: "#c8c5bd", label: [11, 13, 78, 58], shape: "gb" , sprite: "gb" },
  // Wider than tall, and NO notch on the front — the shape detector is a cut on a bottom REAR
  // corner, which you never see from here.
  "Game Boy Advance":    { kind: "cart", w: 140, h: 86,  d: 12, shell: "#5b5f6b", label: [8, 11, 84, 70], shape: "gba" , sprite: "gba" },
  "Nintendo DS":         { kind: "card", w: 118, h: 102, d: 7,  shell: "#3a3d45", label: [8, 10, 84, 70], shape: "dscard" , sprite: "ds" },
  "Nintendo 3DS":        { kind: "card", w: 118, h: 102, d: 7,  shell: "#2e3138", label: [8, 10, 84, 70], shape: "dscard" , sprite: "n3ds" },
  // The Switch card is tiny, red, and its corner is clipped.
  "Nintendo Switch":     { kind: "card", w: 92,  h: 108, d: 6,  shell: "#c0392b", label: [9, 9, 82, 76], shape: "switch" , sprite: "switch" },
  "Nintendo Switch 2":   { kind: "card", w: 92,  h: 108, d: 6,  shell: "#8e2a20", label: [9, 9, 82, 76], shape: "switch" , sprite: "switch" },
  // Genesis: tall, black, and the grip ridges are a band across the top of the face.
  "Sega Genesis":        { kind: "cart", w: 105, h: 134, d: 20, shell: "#26282d", label: [10, 22, 80, 56], shape: "genesis" , sprite: "genesis" },
  "Sega Master System":  { kind: "card", w: 128, h: 108, d: 9,  shell: "#26282d", label: [9, 11, 82, 66], shape: "sms" },
  "Game Gear":           { kind: "cart", w: 118, h: 92,  d: 14, shell: "#2b2d32", label: [9, 11, 82, 68], shape: "gba" },
  "Virtual Boy":         { kind: "cart", w: 112, h: 116, d: 16, shell: "#7a1f1f", label: [11, 13, 78, 58], shape: "gb" },
  "Nintendo Virtual Boy":{ kind: "cart", w: 112, h: 116, d: 16, shell: "#7a1f1f", label: [11, 13, 78, 58], shape: "gb" },
  "Atari 2600":          { kind: "cart", w: 124, h: 100, d: 24, shell: "#2a2622", label: [10, 16, 80, 52], shape: "atari" },
  "Neo-Geo":             { kind: "cart", w: 142, h: 118, d: 26, shell: "#1f2126", label: [8, 13, 84, 60], shape: "neogeo" },
  "TurboGrafx-16":       { kind: "card", w: 112, h: 94,  d: 5,  shell: "#3a3d45", label: [7, 8, 86, 78], shape: "dscard" },
  "WonderSwan":          { kind: "cart", w: 100, h: 100, d: 10, shell: "#4a4d55", label: [11, 11, 78, 66], shape: "gb" },

  // Optical. `hub` is the clear centre ring; discs get a sheen and a hole.
  "PlayStation":         { kind: "disc", size: 150, tint: "#8c8f96" },
  "PlayStation 2":       { kind: "disc", size: 150, tint: "#2b3a6b" },
  "PlayStation 3":       { kind: "disc", size: 150, tint: "#7f8286" },
  "PlayStation 4":       { kind: "disc", size: 150, tint: "#2f6fb5" },
  "PlayStation 5":       { kind: "disc", size: 150, tint: "#2f6fb5" },
  "PlayStation Portable":{ kind: "umd",  size: 108, tint: "#3a3d45" },
  "Nintendo GameCube":   { kind: "disc", size: 112, tint: "#6f42a0", mini: true },   // 8cm mini-disc
  "Nintendo Wii":        { kind: "disc", size: 150, tint: "#dfe3e8" },
  "Nintendo Wii U":      { kind: "disc", size: 150, tint: "#4a86c8" },
  "Xbox":                { kind: "disc", size: 150, tint: "#2e7d32" },
  "Xbox 360":            { kind: "disc", size: 150, tint: "#5aa02c" },
  "Xbox One":            { kind: "disc", size: 150, tint: "#2f6fb5" },
  "Xbox Series X|S":     { kind: "disc", size: 150, tint: "#2f6fb5" },
  "Sega Dreamcast":      { kind: "disc", size: 150, tint: "#d94f2b" },
  "Sega Saturn":         { kind: "disc", size: 150, tint: "#6f8296" },
  "Sega CD":             { kind: "disc", size: 150, tint: "#6f8296" },
  "3DO":                 { kind: "disc", size: 150, tint: "#6f8296" },
  "PC":                  { kind: "disc", size: 150, tint: "#8c8f96" },
};

const mediaFor = (platform) => MEDIA[(platform || "").trim()] || null;

/* Is there anything actually IN the box?

   A derived label on a generic shell is a GUESS, not contents. Offering "open the box" and then
   showing a shell with a cropped cover stuck to it promises something we haven't got — so the
   button only appears when we have fetched something real: a genuine disc scan, a genuine media
   scan (ScreenScraper, when its dev key lands), or the actual manual.

   Once you're in — because there IS a manual — the model still gets its derived label, because
   the alternative is an empty shell next to the booklet, and that's worse. The provenance line
   says plainly which it is. */
function hasBoxContents(mk) {
  const e = (typeof ENRICH !== "undefined" && ENRICH[mk]) || {};
  return !!(e.discArt || e.mediaArt || e.manualEmbed);
}

// The art for the media face, best first.
function mediaArt(mk) {
  const e = (typeof ENRICH !== "undefined" && ENRICH[mk]) || {};
  return {
    // A real scan of the printed disc — GameTDB. Nothing else in the app has this.
    disc: cImg(e.discArt) || null,
    // Reserved for ScreenScraper's `support` media when the dev key lands.
    scan: cImg(e.mediaArt) || null,
    // Everything else derives from the cover. A cart label usually IS a crop of the cover,
    // which is why this holds up.
    cover: coverSrc(e, "cover_big") || null,
    manual: e.manualEmbed || null,
    manualUrl: e.manualUrl || null,
    // The PDF itself, cached on the PVC — when the Archive item has one we page
    // through our own copy (instant on a repeat open, works offline) instead of
    // booting their BookReader over the network. Falls back to the embed below.
    manualPdf: e.manualPdf || null,
    // How thick the booklet is — a 4-page leaflet and a 64-page JRPG tome are different
    // propositions, and you want to know which before you open it.
    manualPages: e.manualPages || null,
  };
}

/* ---- the rendered shells ---------------------------------------------------

   Six CSS faces with clip-paths cannot describe a cartridge. A real one has chamfers, draft angles
   and a recessed label well, so every tweak made it a slightly different wrong shape. So the shells
   are MODELLED and pre-rendered by tools/render_carts.py: 24 frames of a slow turn, each with the
   label area punched out to transparent, plus the four corners of that hole in image space.

   Here we play the frames and warp the game's own label into the hole with a homography. Because
   the label goes UNDERNEATH the frame, the shell's bevel and ribs draw over its edges — the label
   is occluded by the plastic around it for free, which is the thing that sells it.

   Which way the box opens is decided by what's in it: a bare cart never lived in a hinged case, so
   retro carts SLIDE OUT of the sleeve; cards (DS, 3DS, Switch) and discs are in hinged cases. */
const opensBy = (platform) => (mediaFor(platform)?.kind === "cart" ? "slide" : "hinge");

const _cartMeta = {};
const cartMeta = (id) => (_cartMeta[id] ||= fetch(`carts/${id}.json`).then((r) => r.json()));

/* Map the label element's own rectangle onto the four corners of the punched-out hole.

   This is the projective (not affine) transform — the hole is a quad in perspective, so parallel
   edges do NOT stay parallel, and a scale+skew cannot reach it. Solve for the 3x3 and hand CSS the
   matrix3d; the trailing scale() is what turns the element's pixel coords into the unit square the
   solution is written in. */
function homography(w, h, q) {
  const [p0, p1, p2, p3] = q;                                   // TL, TR, BR, BL
  const dx1 = p1[0] - p2[0], dx2 = p3[0] - p2[0], dx3 = p0[0] - p1[0] + p2[0] - p3[0];
  const dy1 = p1[1] - p2[1], dy2 = p3[1] - p2[1], dy3 = p0[1] - p1[1] + p2[1] - p3[1];
  const den = dx1 * dy2 - dx2 * dy1;
  let g = 0, i = 0;
  if (Math.abs(den) > 1e-9 && (Math.abs(dx3) > 1e-9 || Math.abs(dy3) > 1e-9)) {
    g = (dx3 * dy2 - dx2 * dy3) / den;
    i = (dx1 * dy3 - dx3 * dy1) / den;
  }
  const a = p1[0] - p0[0] + g * p1[0], b = p3[0] - p0[0] + i * p3[0], c = p0[0];
  const d = p1[1] - p0[1] + g * p1[1], e = p3[1] - p0[1] + i * p3[1], f = p0[1];
  return `matrix3d(${a},${d},0,${g},${b},${e},0,${i},0,0,1,0,${c},${f},0,1)`
       + ` scale(${1 / w},${1 / h})`;
}

const LAB = 200;                     // the label element's own size; the homography does the rest

function shellHtml(m, art, title) {
  const face = art.scan || art.cover;
  const lab = face
    ? `<span class="md-lab" style="background-image:url('${escapeHtml(face)}')"></span>`
    : `<span class="md-lab blank"><b>${escapeHtml((title || "").slice(0, 24))}</b></span>`;
  return `<div class="md-shell" data-cart="${m.sprite}"
    style="--sheet:url('carts/${m.sprite}.png');--lab:${LAB}px">${lab}
    <span class="md-frames"></span></div>`;
}

/* Start (or restart) every rendered shell under `root`. Idle sway, and you can grab it and turn it.
   The interval stops itself once the element leaves the document — boxes get opened and closed a
   lot, and a timer per open box that never dies is a leak with a long fuse. */
function mountShells(root) {
  (root || document).querySelectorAll(".md-shell[data-cart]").forEach(async (el) => {
    if (el._mounted) return;
    el._mounted = true;
    const meta = await cartMeta(el.dataset.cart);
    const frames = el.querySelector(".md-frames");
    const lab = el.querySelector(".md-lab");

    /* The label element has to be the SHAPE OF THE WELL it's going into. Leave it square and
       `background-size: cover` crops the art to a square, which the homography then squashes into
       a wide well — every GBA label came out stretched. Size it to the well's own aspect and the
       crop happens in the right proportion, before the warp. */
    const lw = LAB, lh = Math.round(LAB / meta.aspect);
    lab.style.width = lw + "px";
    lab.style.height = lh + "px";
    let t = 0, grab = null;

    const paint = () => {
      const i = ((Math.round(t) % meta.frames) + meta.frames) % meta.frames;
      frames.style.backgroundPosition = `${(i / (meta.frames - 1)) * 100}% 0`;
      const k = (el.clientWidth || 1) / meta.size;
      lab.style.opacity = meta.front[i] ? "1" : "0";
      lab.style.transform = homography(lw, lh, meta.quads[i].map(([x, y]) => [x * k, y * k]));
    };

    el.addEventListener("pointerdown", (e) => {
      grab = { x: e.clientX, t };
      el.setPointerCapture(e.pointerId);
      el.classList.add("grabbed");
    });
    el.addEventListener("pointermove", (e) => {
      if (!grab) return;
      t = grab.t + (e.clientX - grab.x) / 9;      // turn it with your finger
      paint();
    });
    const drop = () => { grab = null; el.classList.remove("grabbed"); };
    el.addEventListener("pointerup", drop);
    el.addEventListener("pointercancel", drop);

    const tick = setInterval(() => {
      if (!el.isConnected) return clearInterval(tick);
      if (grab) return;
      t += 1;
      paint();
    }, 110);
    paint();
  });
}

// ---- the models ------------------------------------------------------------
function cartHtml(m, art, title) {
  if (m.sprite) return shellHtml(m, art, title);
  const face = art.scan || art.cover;
  const [lx, ly, lw, lh] = m.label;
  // The label is a CROP of the cover, not the whole cover squashed onto a sticker — a squashed
  // cover reads as a printing error. Scale it up and pull the middle out.
  const label = face
    ? `<span class="md-label" style="left:${lx}%;top:${ly}%;width:${lw}%;height:${lh}%;
         background-image:url('${escapeHtml(face)}')"></span>`
    : `<span class="md-label blank" style="left:${lx}%;top:${ly}%;width:${lw}%;height:${lh}%">
         <b>${escapeHtml((title || "").slice(0, 22))}</b></span>`;
  // One class per machine — the shell details live in CSS, where each is that console's cart and
  // not a generic slab with a flag or two flipped.
  const cls = ["md-cart", m.kind === "card" ? "card" : "", `s-${m.shape || "plain"}`].join(" ");
  return `<div class="${cls}" style="--mw:${m.w}px;--mh:${m.h}px;--md:${m.d}px;--shell:${m.shell}">
    <span class="md-f front">${label}</span>
    <span class="md-f back"></span>
    <span class="md-f top"></span>
    <span class="md-f bottom"></span>
    <span class="md-f left"></span>
    <span class="md-f right"></span>
  </div>`;
}

function discHtml(m, art) {
  // A REAL disc scan if we have one; otherwise the cover, masked into the ring — which is what a
  // printed disc largely is anyway.
  const face = art.disc || art.scan || art.cover;
  const kind = m.kind === "umd" ? "md-umd" : "md-disc";
  return `<div class="${kind}${m.mini ? " mini" : ""}"
    style="--ms:${m.size}px;--tint:${m.tint}${face ? `;--face:url('${escapeHtml(face)}')` : ""}">
    ${m.kind === "umd"
      // The UMD is a flat card with nothing behind it, so its sheen can stay a blended child.
      // A DISC cannot afford one — it has a read side to show, and blending flattens the 3D it
      // needs to hide behind the label. Its rainbow lives in its own background (see .md-disc).
      ? `<span class="md-sheen"></span><span class="md-umd-shell"></span>`
      : `<span class="md-hub"></span><span class="md-under"></span>`}
  </div>`;
}

// Just the object, for seating INSIDE the 3D case (see shBuild).
function mediaModelHtml(g) {
  const m = mediaFor(g.p);
  if (!m) return "";
  const art = mediaArt(g.mk);
  return (m.kind === "disc" || m.kind === "umd") ? discHtml(m, art) : cartHtml(m, art, g.t);
}

/* The panel that appears when you open a box. */
function mediaPanelHtml(g) {
  const m = mediaFor(g.p);
  const art = mediaArt(g.mk);
  if (!m && !art.manual) return "";

  const model = !m ? ""
    : (m.kind === "disc" || m.kind === "umd") ? discHtml(m, art) : cartHtml(m, art, g.t);

  // Say where the art came from. A derived label is a guess and shouldn't pretend otherwise.
  const provenance = !m ? ""
    : art.disc ? `<span class="md-src real">Real disc scan · GameTDB</span>`
    : art.scan ? `<span class="md-src real">Real scan</span>`
    : art.cover ? `<span class="md-src derived">Label from the box art</span>`
    : `<span class="md-src none">No art</span>`;

  return `<div class="md-panel" id="mdPanel">
    <div class="md-stage">${model}</div>
    <div class="md-side">
      ${m ? `<div class="md-what">${escapeHtml(mediaName(m, g.p))}</div>${provenance}` : ""}
      ${art.manual
        ? `<button class="sh-btn primary" id="mdManual">${icon("i-review", 14)} Read the manual</button>
           <span class="md-src">Internet Archive${art.manualPages ? ` · ${art.manualPages} pages` : ""}</span>`
        : `<span class="md-src none">No manual found</span>`}
    </div>
  </div>`;
}

const mediaName = (m, platform) =>
  m.kind === "disc" ? (m.mini ? "GameCube mini-disc" : "Disc")
  : m.kind === "umd" ? "UMD"
  : m.kind === "card" ? "Game card"
  : `${platform} cartridge`;

/* The manual itself. The Internet Archive's BookReader pages, zooms and searches a scan already —
   building a PDF viewer to re-render something they already render would be daft. */
function openManual(g) {
  const art = mediaArt(g.mk);
  if (!art.manual && !art.manualPdf) return;
  // Prefer our PVC-cached PDF in the browser's own viewer: a booklet opened before
  // comes off local disk in a blink. Only when the Archive item has no PDF do we
  // fall back to their BookReader embed (which boots over the network every time).
  const pdf = art.manualPdf ? cManual(art.manualPdf) : "";
  const src = pdf ? `${pdf}#view=FitH` : art.manual;
  const say = pdf ? "Loading the booklet…" : "Fetching the booklet from the Internet Archive…";
  const host = document.createElement("div");
  host.className = "md-scrim";
  host.innerHTML = `
    <div class="md-book" role="dialog" aria-label="Manual for ${escapeHtml(g.t)}">
      <div class="md-book-bar">
        <b>${escapeHtml(g.t)}</b>
        <span class="muted">Instruction booklet · Internet Archive</span>
        <a class="sh-btn" href="${escapeHtml(art.manualUrl)}" target="_blank" rel="noopener">Open at the Archive ↗</a>
        <button class="sh-btn" id="mdClose">Close</button>
      </div>
      <div class="md-skel" aria-hidden="true">
        <div class="md-skel-page"><i></i><i></i><i></i><i></i><i></i></div>
        <div class="md-skel-page"><i></i><i></i><i></i><i></i><i></i></div>
        <span class="md-skel-say">${say}</span>
      </div>
      <iframe src="${escapeHtml(src)}" allowfullscreen frameborder="0"></iframe>
    </div>`;
  document.body.appendChild(host);
  /* The Archive's BookReader takes a few seconds to boot, and until it does the iframe is a blank
     white rectangle that reads as broken. Hold a pair of skeleton pages over it until it loads —
     and drop them on `load` whether or not it succeeded, so a failure shows the reader's own error
     rather than a shimmer that never ends. */
  const frame = host.querySelector("iframe");
  const skel = host.querySelector(".md-skel");
  frame.addEventListener("load", () => skel.classList.add("gone"), { once: true });
  setTimeout(() => skel.classList.add("gone"), 12000);      // never shimmer forever
  syncScrollLock?.();
  const close = () => { host.remove(); syncScrollLock?.(); };
  host.querySelector("#mdClose").onclick = close;
  host.addEventListener("click", (e) => { if (e.target === host) close(); });
}
