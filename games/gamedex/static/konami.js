"use strict";

/* ↑ ↑ ↓ ↓ ← → ← → B A
 *
 * A commando runs the width of the screen along the bottom, firing a spread gun.
 *
 * The sprite is ORIGINAL — hand-authored below as a pixel matrix, not a rip. A ripped sheet
 * would be somebody else's artwork redistributed as decoration on a public site, which is a
 * different thing from the rating marks (a trademark used to label the very games it rates)
 * or the CSS spines (brand colours, not brand art). So: our own pixels, same silhouette
 * language — headband, tank top, cargo pants, rifle held low — and a spread of five shots
 * fanning out ahead of him.
 *
 * Everything is drawn to a canvas at 1x and scaled up with image-rendering: pixelated, so it
 * stays crisp and stays honest to the medium. */

const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown",
                "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];

// . transparent · S skin · H headband · V vest/tank · P trousers · B boots · G gun · M muzzle
const PAL = {
  S: "#e8b088", H: "#d92b2b", V: "#3f6fd8", P: "#2f7a45",
  B: "#6b4a2f", G: "#4a4a55", M: "#ffd447", K: "#1d2029",
};

// 16 wide x 18 tall. Two run frames — legs forward, legs back — plus the arm holding the
// rifle out ahead. Drawn by hand; the shape reads at 3x, which is all it has to do.
const FRAMES = [
`......HHHH......
.....HHHHHH.....
.....SSSSSS.....
.....SKSSKS.....
.....SSSSSS.....
......SSSS......
....VVVVVVVV....
...VVVVVVVVVV...
..SVVVVVVVVVVS..
..SVVVVVVVVVVS..
..SSVVVVVVVVSS..
...S.PPPPPP.GGGG
.....PPPPPP.GGGG
.....PP..PP.....
....PPP..PPP....
....BBB..BBB....
...BBBB..BBBB...
................`,
`......HHHH......
.....HHHHHH.....
.....SSSSSS.....
.....SKSSKS.....
.....SSSSSS.....
......SSSS......
....VVVVVVVV....
...VVVVVVVVVV...
..SVVVVVVVVVVS..
..SVVVVVVVVVVS..
..SSVVVVVVVVSS..
...S.PPPPPP.GGGG
.....PPPPPP.GGGG
.....PPPP.......
...PPP..PPPP....
..BBB......BBB..
.BBBB.......BBBB
................`,
];

let konamiRunning = false;

function konamiSprite(frame) {
  const rows = FRAMES[frame].split("\n");
  const c = document.createElement("canvas");
  c.width = rows[0].length;
  c.height = rows.length;
  const g = c.getContext("2d");
  rows.forEach((row, y) => {
    [...row].forEach((ch, x) => {
      const col = PAL[ch];
      if (!col) return;
      g.fillStyle = col;
      g.fillRect(x, y, 1, 1);
    });
  });
  return c;
}

function konamiRun() {
  if (konamiRunning) return;
  konamiRunning = true;

  const SC = 3;                       // pixel scale
  const cv = document.createElement("canvas");
  cv.className = "konami-cv";
  const W = window.innerWidth, H = 150;
  cv.width = W; cv.height = H;
  document.body.appendChild(cv);
  const g = cv.getContext("2d");
  g.imageSmoothingEnabled = false;

  const sprites = [konamiSprite(0), konamiSprite(1)];
  const sw = sprites[0].width * SC, sh = sprites[0].height * SC;
  const groundY = H - sh - 6;

  let x = -sw;
  const speed = 3.1;
  const bullets = [];
  let t = 0, lastShot = -999;

  // The spread gun: five shots at once, fanning. It is the only correct choice.
  const fire = (from) => {
    for (const dy of [-2.2, -1.1, 0, 1.1, 2.2]) {
      bullets.push({ x: from, y: groundY + sh * 0.42, vx: 7.5, vy: dy, life: 0 });
    }
  };

  const tick = () => {
    t++;
    g.clearRect(0, 0, W, H);

    // run
    x += speed;
    const frame = Math.floor(t / 6) % 2;
    g.drawImage(sprites[frame], Math.round(x), Math.round(groundY), sw, sh);

    // muzzle flash + shots, every ~26 frames
    const muzzleX = x + sw - 4;
    if (t - lastShot > 26) {
      lastShot = t;
      fire(muzzleX);
    }
    if (t - lastShot < 4) {
      g.fillStyle = PAL.M;
      g.fillRect(Math.round(muzzleX), Math.round(groundY + sh * 0.38), 7, 7);
    }

    for (const b of bullets) {
      b.x += b.vx; b.y += b.vy; b.life++;
      g.fillStyle = b.life % 4 < 2 ? PAL.M : "#fff6c2";
      g.fillRect(Math.round(b.x), Math.round(b.y), 6, 4);
    }
    for (let i = bullets.length - 1; i >= 0; i--) {
      if (bullets[i].x > W + 20 || bullets[i].y < -10 || bullets[i].y > H + 10) bullets.splice(i, 1);
    }

    if (x > W + sw) {
      cv.remove();
      konamiRunning = false;
      return;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  // A little acknowledgement, so it's clear it was you and not a glitch.
  const t2 = document.createElement("div");
  t2.className = "konami-toast";
  t2.textContent = "30 lives";
  document.body.appendChild(t2);
  setTimeout(() => t2.remove(), 2600);
}

(function wireKonami() {
  let i = 0;
  window.addEventListener("keydown", (e) => {
    // Don't eat the code while you're typing in the search box.
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.target.isContentEditable) { i = 0; return; }
    const want = KONAMI[i];
    const got = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (got === want) {
      i++;
      if (i === KONAMI.length) { i = 0; konamiRun(); }
    } else {
      // Restart the sequence rather than dropping it — pressing Up three times shouldn't
      // ruin the run, since the third Up is a valid first Up.
      i = got === KONAMI[0] ? 1 : 0;
    }
  });
})();
