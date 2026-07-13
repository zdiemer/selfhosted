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
  B: "#6b4a2f", G: "#3c3c46", M: "#ffd447", K: "#1d2029",
};

/* Side view, facing right, mid-stride, rifle held out in both hands.
 *
 * The first pass was drawn front-on — two arms down, two legs side by side — so he read as a
 * man standing still and facing you while sliding across the floor, and the "gun" was a grey
 * smudge behind his hip. A runner has to be in PROFILE: torso pitched forward over the front
 * foot, one leg driving, the other trailing behind, the headband streaming back off the head.
 * The rifle reads because it's a long horizontal bar held at chest height with a visible hand
 * on the grip and a stock tucked back under the rear arm.
 *
 * Rows are padded to a common width, so I can draw them at whatever length is convenient
 * rather than counting dots. */
/* A FOUR-frame run cycle. Two frames can only ever shuffle: the legs swap between two poses
 * and the eye reads a man shifting his weight, not a man running. A run needs the full cycle —
 * CONTACT (legs scissored wide, front heel down, back toe off), PASSING (legs together under
 * the hips, one knee coming through), CONTACT again on the other leg, PASSING back. That's
 * what makes the legs look like they're driving him forward instead of paddling.
 *
 * The body is the same in all four apart from a one-pixel bob on the passing frames — the head
 * rises as the legs come under you, and it's the bob that sells the weight. */
const BODY = `.......HHHHH.........
....HHHHHHHHH........
...HH..SSSSSSS.......
.......SSKSSSS.......
.......SSSSSSSS......
........SSSSSS.......
.........SSSS........
.....VVVVVVVVV.......
....VVVVVVVVVVSS.....
...VVVVVVVVVVVSSGGGGGGGGG
...VVVVVVVVVVVVSGGGGGGGGG
....VVVVVVVVVVS......`;

/* Seven rows of leg, and the thing that actually makes it a run: ON THE PASSING FRAMES ONE
 * FOOT IS OFF THE FLOOR. The last pass had both boots planted on the bottom row in all four
 * frames — legs opening and closing like scissors while the feet never left the ground, which
 * is precisely what a shuffle IS. A run is defined by the moment a foot leaves the floor: the
 * knee drives up, the boot hangs in the air, and the other leg carries the whole body. Draw
 * that and the legs read as driving; leave it out and no amount of extra frames will help. */
const LEGS = [
  // 1 · CONTACT — scissored wide, front heel landing ahead, rear leg trailing far behind
  `.....PPPPPPPP........
...PPPP....PPPP......
..PPP........PPPP....
.PPP...........PPP...
.PPP............PPP..
BBBB.............PPP.
BBBB............BBBB.`,
  // 2 · PASSING — rear knee driven up through the middle, THAT BOOT IS IN THE AIR, and the
  //     support leg alone is holding him up
  `.....PPPPPPPP........
...PPPPPPPPPP........
..PPPP...PPPPP.......
..PPP......PPP.......
.BBBB......PPP.......
...........PPP.......
..........BBBB.......`,
  // 3 · CONTACT — the other leg lands; stride tighter, front knee still bent from the drive
  `.....PPPPPPPP........
...PPPPP...PPPP......
..PPP.......PPPP.....
..PPP.........PPP....
..PPP..........PPP...
.BBBB...........PPP..
BBBB...........BBBB..`,
  // 4 · PASSING — front knee up, front boot in the air, rear leg carrying him
  `.....PPPPPPPP........
....PPPPPPPPPP.......
....PPPP..PPPPP......
....PPP.....PPPP.....
....PPP......BBBB....
....PPP..............
...BBBB..............`,
];

// The passing frames sit a pixel higher — that's the bob.
const FRAMES = LEGS.map((legs, i) =>
  (i % 2 === 1 ? "" : ".....................\n") + BODY + "\n" + legs);

let konamiRunning = false;

function konamiSprite(frame) {
  // NOT trim() — the leading blank row on the contact frames IS the bob, and trimming it
  // would quietly delete the very thing that gives the run its weight.
  const rows = FRAMES[frame].replace(/^\n|\n$/g, "").split("\n");
  const w = Math.max(...rows.map((r) => r.length));
  const c = document.createElement("canvas");
  c.width = w;
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

  const SC = 4;                       // pixel scale — big enough to read the stride
  const cv = document.createElement("canvas");
  cv.className = "konami-cv";
  const W = window.innerWidth, H = 150;
  cv.width = W; cv.height = H;
  document.body.appendChild(cv);
  const g = cv.getContext("2d");
  g.imageSmoothingEnabled = false;

  const sprites = FRAMES.map((_, i) => konamiSprite(i));
  const sw = sprites[0].width * SC, sh = sprites[0].height * SC;
  const groundY = H - sh - 6;

  let x = -sw;
  const speed = 3.1;
  const bullets = [];
  let t = 0, lastShot = -999;

  // The spread gun: five shots at once, fanning. It is the only correct choice.
  const fire = (from) => {
    for (const dy of [-2.2, -1.1, 0, 1.1, 2.2]) {
      bullets.push({ x: from, y: groundY + sh * 0.55, vx: 7.5, vy: dy, life: 0 });
    }
  };

  const tick = () => {
    t++;
    g.clearRect(0, 0, W, H);

    // run
    x += speed;
    const frame = Math.floor(t / 5) % sprites.length;
    g.drawImage(sprites[frame], Math.round(x), Math.round(groundY), sw, sh);

    // muzzle flash + shots, every ~26 frames
    const muzzleX = x + sw - 4;
    if (t - lastShot > 26) {
      lastShot = t;
      fire(muzzleX);
    }
    if (t - lastShot < 4) {
      g.fillStyle = PAL.M;
      g.fillRect(Math.round(muzzleX), Math.round(groundY + sh * 0.51), 8, 8);
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
