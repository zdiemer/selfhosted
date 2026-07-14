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
  "NES":                 { kind: "cart", w: 115, h: 128, d: 20, shell: "#b8b3a7", label: [12, 14, 76, 56], shape: "nes" },
  "Nintendo Entertainment System": { kind: "cart", w: 115, h: 128, d: 20, shell: "#b8b3a7", label: [12, 14, 76, 56], shape: "nes" },
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
  "SNES":                { kind: "cart", w: 132, h: 121, d: 20, shell: "#b9b5ad", label: [17, 16, 66, 54], shape: "snes" },
  "Super Nintendo":      { kind: "cart", w: 132, h: 121, d: 20, shell: "#b9b5ad", label: [17, 16, 66, 54], shape: "snes" },
  "Nintendo 64":         { kind: "cart", w: 92,  h: 135, d: 20, shell: "#33353b", label: [11, 11, 78, 52], shape: "n64" },
  "Game Boy":            { kind: "cart", w: 105, h: 121, d: 14, shell: "#c8c5bd", label: [11, 13, 78, 58], shape: "gb" },
  "Game Boy Color":      { kind: "cart", w: 105, h: 121, d: 14, shell: "#c8c5bd", label: [11, 13, 78, 58], shape: "gb" },
  // Wider than tall, and NO notch on the front — the shape detector is a cut on a bottom REAR
  // corner, which you never see from here.
  "Game Boy Advance":    { kind: "cart", w: 140, h: 86,  d: 12, shell: "#5b5f6b", label: [8, 11, 84, 70], shape: "gba" },
  "Nintendo DS":         { kind: "card", w: 118, h: 102, d: 7,  shell: "#3a3d45", label: [8, 10, 84, 70], shape: "dscard" },
  "Nintendo 3DS":        { kind: "card", w: 118, h: 102, d: 7,  shell: "#2e3138", label: [8, 10, 84, 70], shape: "dscard" },
  // The Switch card is tiny, red, and its corner is clipped.
  "Nintendo Switch":     { kind: "card", w: 92,  h: 108, d: 6,  shell: "#c0392b", label: [9, 9, 82, 76], shape: "switch" },
  "Nintendo Switch 2":   { kind: "card", w: 92,  h: 108, d: 6,  shell: "#8e2a20", label: [9, 9, 82, 76], shape: "switch" },
  // Genesis: tall, black, and the grip ridges are a band across the top of the face.
  "Sega Genesis":        { kind: "cart", w: 105, h: 134, d: 20, shell: "#26282d", label: [10, 22, 80, 56], shape: "genesis" },
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
    disc: e.discArt || null,
    // Reserved for ScreenScraper's `support` media when the dev key lands.
    scan: e.mediaArt || null,
    // Everything else derives from the cover. A cart label usually IS a crop of the cover,
    // which is why this holds up.
    cover: coverSrc(e, "cover_big") || null,
    manual: e.manualEmbed || null,
    manualUrl: e.manualUrl || null,
  };
}

// ---- the models ------------------------------------------------------------
function cartHtml(m, art, title) {
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
    <span class="md-sheen"></span>
    ${m.kind === "umd" ? `<span class="md-umd-shell"></span>` : `<span class="md-hub"></span>`}
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
           <span class="md-src">Internet Archive</span>`
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
  if (!art.manual) return;
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
      <iframe src="${escapeHtml(art.manual)}" allowfullscreen frameborder="0"></iframe>
    </div>`;
  document.body.appendChild(host);
  syncScrollLock?.();
  const close = () => { host.remove(); syncScrollLock?.(); };
  host.querySelector("#mdClose").onclick = close;
  host.addEventListener("click", (e) => { if (e.target === host) close(); });
}
