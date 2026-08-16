#!/usr/bin/env python3
"""Draw site/img/og.jpg — the link-preview card for luke.diemer.codes.

Kept in the repo because the card is generated, not hand-drawn: if the page's
joke changes, the preview should change with it, and a JPEG in git with no
source is a dead end. Run from this directory:

    python3 gen-og.py

Everything is drawn with Pillow's bundled font (Aileron) — this container has no
system fonts at all, so `truetype("Impact")` is not an option. "Bold" below is
therefore faked by stamping the same string at small offsets, which is also
period-appropriate.

1200x630 is the size Facebook/iMessage/Signal all crop least aggressively. The
important half of the composition stays inside the middle ~1000x500 so a square
crop in a group chat still lands on Luke's face and the word THIRTY.
"""

import math
import random

from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 1200, 630
F = ImageFont.load_default


def bold(d, xy, txt, font, fill, weight=2, anchor=None):
    """Fake a bold weight by overstamping. Pillow's default font has one face."""
    x, y = xy
    for dx in range(-weight, weight + 1):
        for dy in range(-weight, weight + 1):
            d.text((x + dx, y + dy), txt, font=font, fill=fill, anchor=anchor)


def shadowed(d, xy, txt, font, fill, shadow, depth=6, anchor=None):
    """1998 WordArt: a hard drop shadow stepped down-right, no blur."""
    x, y = xy
    for i in range(depth, 0, -1):
        d.text((x + i, y + i), txt, font=font, fill=shadow, anchor=anchor)
    bold(d, (x, y), txt, font, fill, weight=2, anchor=anchor)


img = Image.new("RGB", (W, H), "#05010f")
d = ImageDraw.Draw(img)

# ---- starfield, same generator as the page background -----------------------
random.seed(1996)
for _ in range(420):
    x, y = random.randrange(W), random.randrange(H)
    s = random.choice([0, 0, 0, 1, 2])
    d.rectangle([x, y, x + s, y + s], fill=random.choice(["#ffffff", "#aaccff", "#ffddaa", "#8899ff"]))

# ---- construction stripes, top and bottom -----------------------------------
for y0 in (0, H - 26):
    d.rectangle([0, y0, W, y0 + 26], fill="#ffcc00")
    for x in range(-40, W + 40, 40):
        d.polygon([(x, y0 + 26), (x + 20, y0 + 26), (x + 46, y0), (x + 26, y0)], fill="#111111")

# ---- rainbow rules ----------------------------------------------------------
def rainbow_rule(y):
    for x in range(W):
        t = x / 40.0
        c = (
            int(127 + 128 * math.sin(t)),
            int(127 + 128 * math.sin(t + 2.09)),
            int(127 + 128 * math.sin(t + 4.18)),
        )
        d.line([(x, y), (x, y + 7)], fill=c)


rainbow_rule(34)
rainbow_rule(H - 42)

# ---- Luke, framed -----------------------------------------------------------
photo = Image.open("site/img/luke-square.jpg").convert("RGB")
photo = ImageOps.fit(photo, (300, 300), Image.LANCZOS, centering=(0.5, 0.35))
frame = Image.new("RGB", (320, 320), "#ffcc00")
ImageDraw.Draw(frame).rectangle([6, 6, 313, 313], outline="#ff0066", width=3)
frame.paste(photo, (10, 10))
img.paste(frame, (58, 132))

# A starburst badge over the corner of the photo, because of course.
star_c = (352, 160)
pts = []
for i in range(20):
    rad = 62 if i % 2 == 0 else 27
    a = i * math.pi / 10 - math.pi / 2
    pts.append((star_c[0] + rad * math.cos(a), star_c[1] + rad * math.sin(a)))
d.polygon(pts, fill="#ffe000", outline="#ff0000")
bold(d, star_c, "30", F(46), "#cc0000", weight=2, anchor="mm")

# ---- the words --------------------------------------------------------------
TX = 420
shadowed(d, (TX, 92), "HAPPY 30th", F(78), "#ffe000", "#aa0044")
shadowed(d, (TX, 178), "BIRTHDAY LUKE", F(78), "#ffe000", "#aa0044")

d.rectangle([TX, 286, TX - 4 + 700, 336], fill="#000080", outline="#6666ff", width=3)
bold(d, (TX + 20, 296), "*** IT IS HIS BIRTHDAY. TODAY. ***", F(26), "#ffff66")

lines = [
    ">  30 years of gaining elevation on purpose",
    "> Denver, CO  *  aerospace  *  hiking  *  skiing",
    "> currently charging the teleporter",
]
y = 358
for line in lines:
    d.text((TX, y), line, font=F(25), fill="#66ddff")
    y += 36

# ---- the little green terminal box, bottom right ----------------------------
bx0, by0, bx1, by1 = TX, 476, TX + 700, 552
d.rectangle([bx0, by0, bx1, by1], fill="#000000", outline="#888888", width=3)
bold(d, (bx0 + 16, by0 + 10), "YOU ARE VISITOR 0000030", F(26), "#33ff66", weight=1)
d.text((bx0 + 16, by0 + 44), "PLEASE SIGN THE GUESTBOOK  //  luke.diemer.codes", font=F(20), fill="#99ffbb")

# ---- cake, tucked under the photo -------------------------------------------
# The photo frame ends at y=452; the candle flames start at 494, so the two
# never touch however the card is cropped (flames 460, frame bottom 452).
cx, cy = 208, 534
d.rectangle([cx - 62, cy, cx + 62, cy + 44], fill="#ff77bb", outline="#ffffff", width=3)
d.rectangle([cx - 62, cy - 10, cx + 62, cy + 8], fill="#ffffff")
for off in (-38, 0, 38):
    d.rectangle([cx + off - 5, cy - 44, cx + off + 5, cy - 6], fill="#66ccff", outline="#ffffff")
    d.polygon([(cx + off, cy - 74), (cx + off - 10, cy - 46), (cx + off + 10, cy - 46)], fill="#ffcc00")
    d.polygon([(cx + off, cy - 62), (cx + off - 5, cy - 46), (cx + off + 5, cy - 46)], fill="#ff3300")

bold(d, (TX, 566), "BEST VIEWED IN NETSCAPE 4.0 AT 800x600", F(19), "#ffffff", weight=1)

img.save("site/img/og.jpg", quality=88, optimize=True, progressive=True)
print("wrote site/img/og.jpg", img.size)
