#!/usr/bin/env python3
"""Draw site/img/baby*.gif — a dancing baby, in the spirit of the 1996 one.

Deliberately NOT the original. "Baby Cha-Cha" is a copyrighted Character Studio
demo animation, and every copy floating around the web is someone else's file.
Everything else on this page is generated, so this is too: a jointed figure
posed per frame and rendered with Pillow.

Rendered at 4x and downsampled, because Pillow has no antialiasing — at 1x the
limbs come out as staircases.

    python3 gen-baby.py

Produces:
    baby.gif       the dancer, transparent background
    baby-party.gif the same, wearing a party hat
"""

import math

from PIL import Image, ImageDraw

S = 4  # supersample factor
W, H = 116, 184
SKIN = (243, 205, 168)
SKIN_DARK = (206, 158, 118)
DIAPER = (250, 250, 252)
DIAPER_SHADE = (206, 210, 222)
HAIR = (120, 84, 52)


def limb(d, pts, width, color, shade=None):
    """A limb is a thick polyline with round joints. Drawn twice when shaded:
    once fat in the darker tone, once thinner on top, which reads as a lit
    edge without needing a real renderer."""
    if shade:
        d.line(pts, fill=shade, width=int(width * S), joint="curve")
        for p in pts:
            r = width * S / 2
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=shade)
    w2 = width * 0.72
    d.line(pts, fill=color, width=int(w2 * S), joint="curve")
    for p in pts:
        r = w2 * S / 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def joint(origin, angle_deg, length):
    a = math.radians(angle_deg)
    return (origin[0] + length * S * math.sin(a), origin[1] + length * S * math.cos(a))


def frame(phase, hat=False):
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # The cha-cha: weight shifts side to side, hips counter-rotate against the
    # shoulders, arms pump on the opposite beat, and the whole body bobs.
    sway = 9 * math.sin(phase)
    bob = 4 * abs(math.sin(phase))
    # Every other term is a function of sin(phase) alone, which makes the pose
    # at phase p identical to the one at pi-p: the loop mirrors instead of
    # travelling, and Pillow drops the duplicate frames. This cos term is what
    # makes the ten frames ten distinct poses.
    lean = 5 * math.cos(phase)
    hip_tilt = 14 * math.sin(phase)
    shoulder_tilt = -10 * math.sin(phase)

    cx = (W / 2 + sway) * S
    hips_y = (110 - bob) * S
    hips = (cx, hips_y)
    shoulders = (cx - shoulder_tilt * S * 0.4 + lean * S * 0.5, (72 - bob) * S)

    # ---- legs. One knee drives out on each beat, the other takes the weight.
    for side, sign in (("l", -1), ("r", 1)):
        beat = math.sin(phase) * sign
        hip_pt = (hips[0] + sign * 11 * S, hips[1] + hip_tilt * sign * S * 0.3)
        knee = joint(hip_pt, sign * (12 + 16 * max(0, beat)), 26)
        foot = joint(knee, sign * (4 + 6 * max(0, beat)) - sign * 2, 26)
        limb(d, [hip_pt, knee, foot], 13, SKIN, SKIN_DARK)
        # bare foot, turned out
        d.ellipse([foot[0] - 8 * S, foot[1] - 4 * S, foot[0] + 8 * S, foot[1] + 5 * S], fill=SKIN)

    # ---- diaper -------------------------------------------------------------
    d.rounded_rectangle(
        [hips[0] - 20 * S, hips[1] - 18 * S, hips[0] + 20 * S, hips[1] + 13 * S],
        radius=9 * S, fill=DIAPER, outline=DIAPER_SHADE, width=S,
    )

    # ---- torso --------------------------------------------------------------
    limb(d, [hips, shoulders], 32, SKIN, SKIN_DARK)
    # belly highlight, since a baby is mostly belly
    d.ellipse(
        [cx - 15 * S, (80 - bob) * S, cx + 15 * S, (102 - bob) * S],
        fill=SKIN,
    )

    # ---- arms. Opposite beat to the legs, elbows out, hands up. -------------
    for side, sign in (("l", -1), ("r", 1)):
        beat = math.sin(phase + math.pi) * sign
        sh = (shoulders[0] + sign * 15 * S, shoulders[1] + 3 * S)
        elbow = joint(sh, sign * (108 + 26 * beat), 20)
        hand = joint(elbow, sign * (150 + 34 * beat), 18)
        limb(d, [sh, elbow, hand], 11, SKIN, SKIN_DARK)
        d.ellipse([hand[0] - 6 * S, hand[1] - 6 * S, hand[0] + 6 * S, hand[1] + 6 * S], fill=SKIN)

    # ---- head ---------------------------------------------------------------
    head_c = (cx + sway * 0.25 * S + lean * S * 0.8, (48 - bob - abs(lean) * 0.2) * S)
    hr = 25 * S
    d.ellipse([head_c[0] - hr, head_c[1] - hr, head_c[0] + hr, head_c[1] + hr],
              fill=SKIN, outline=SKIN_DARK, width=S)
    # one curl, the only hair a 1996 render could afford
    d.arc([head_c[0] - 9 * S, head_c[1] - 30 * S, head_c[0] + 9 * S, head_c[1] - 16 * S],
          200, 20, fill=HAIR, width=2 * S)
    # eyes + the open mouth of someone enjoying themselves
    for ex in (-9, 9):
        d.ellipse([head_c[0] + (ex - 2.5) * S, head_c[1] - 6 * S,
                   head_c[0] + (ex + 2.5) * S, head_c[1] + 1 * S], fill=(40, 30, 25))
    d.ellipse([head_c[0] - 6 * S, head_c[1] + 7 * S, head_c[0] + 6 * S, head_c[1] + 16 * S],
              fill=(150, 70, 70))
    d.ellipse([head_c[0] - 20 * S, head_c[1] + 4 * S, head_c[0] - 12 * S, head_c[1] + 12 * S],
              fill=(245, 170, 160))
    d.ellipse([head_c[0] + 12 * S, head_c[1] + 4 * S, head_c[0] + 20 * S, head_c[1] + 12 * S],
              fill=(245, 170, 160))

    if hat:
        tip = (head_c[0] + 4 * S, head_c[1] - 46 * S)
        d.polygon([tip, (head_c[0] - 18 * S, head_c[1] - 20 * S),
                   (head_c[0] + 20 * S, head_c[1] - 20 * S)], fill="#ff3388", outline="#ffffff")
        d.ellipse([tip[0] - 5 * S, tip[1] - 5 * S, tip[0] + 5 * S, tip[1] + 5 * S], fill="#ffe000")

    return img.resize((W, H), Image.LANCZOS)


def save(name, hat):
    n = 10
    frames = [frame(2 * math.pi * i / n, hat) for i in range(n)]
    # GIF has 1-bit alpha: quantise, then reserve index 255 for transparency and
    # paint it wherever the RGBA alpha was low. Converting straight to P leaves a
    # black box behind the baby on a dark page.
    out = []
    for f in frames:
        p = f.convert("RGB").quantize(colors=255, method=Image.MEDIANCUT)
        mask = f.getchannel("A").point(lambda a: 255 if a < 128 else 0)
        p.paste(255, mask)
        out.append(p)
    out[0].save(name, save_all=True, append_images=out[1:], duration=90, loop=0,
                transparency=255, disposal=2)
    print("wrote", name)


save("site/img/baby.gif", hat=False)
save("site/img/baby-party.gif", hat=True)
