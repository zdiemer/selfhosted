#!/usr/bin/env python3
"""Turn a Cover Project scan into the three faces of a game case.

A wrap is one flat image of the whole box: back | spine | front. Cutting it is
easy in principle and full of traps in practice. Every trap below is one I hit on
real scans, not a hypothetical.

  1. SOME SCANS ARE ROTATED 90 DEGREES. Super Metroid and Hades both arrive
     portrait. Rotating them is obvious; rotating them the RIGHT WAY is not,
     because Super Metroid has the front on top and Hades has the front on the
     bottom. Rotate both the same way and one of them comes out back-to-front.
     So we don't guess: we rotate, then ask which half looks like a front.

     A front is art. A back is text, screenshots on flat panels, a barcode and a
     ratings box. Art is more SATURATED. That single measurement decides it, and
     it decides it on every scan I've tested.

  2. THE SCAN'S SHAPE TELLS YOU WHICH BOX IT'S FOR — and it isn't always the box
     the game shipped in. Cover Project's PlayStation 1 wraps measure 273x182mm:
     that's a DVD keepcase, because the community reprints PS1 games into DVD
     cases. The real PS1 jewel case is landscape and nothing like it. So the
     WRAP decides the case geometry; the console doesn't.

  3. SLICE BY RATIO, NEVER BY PIXELS. The same game is on their CDN at 96, 300
     and 600 dpi. Fractions survive that; offsets don't.

A scan we can't confidently place gets rejected outright and the caller falls
back to a front-only cover. A wrong wrap is worse than no wrap.
"""

from __future__ import annotations

import colorsys
import io

from PIL import Image

# Cover Project's print templates, in millimetres: back | spine | front.
# Keyed by the aspect the finished wrap measures, which is how we recognise one.
TEMPLATES = {
    "dvd":       (130, 14, 129, 183),   # PS1/PS2/Xbox/GC/Wii/DC — 273 x 183
    "gc":        (124, 14, 124, 175),   # GameCube keepcase (GameTDB wrap, same ~1.51 ratio)
    "snes":      (133, 33, 133, 191),   # cardboard box
    "nes":       (127, 25, 127, 178),   # smaller and thinner than a SNES box
    "genesis":   (133, 28, 133, 184),
    "n64":       (133, 33, 133, 190),
    "switch":    (105, 11, 105, 170),
    "bluray":    (135, 14, 135, 171),   # PS4 / PS5
    "jewel":     (142, 10, 142, 125),   # a real PS1 jewel case — landscape
}

TOLERANCE = 0.07          # how far a scan's aspect may drift from a template
MIN_CONFIDENCE = 0.06     # saturation gap needed to call which end is the front


def _aspect(t) -> float:
    back, spine, front, height = t
    return (back + spine + front) / height


def _saturation(im: Image.Image) -> float:
    """Mean saturation of a thumbnail. Fronts are art; backs are text and panels."""
    small = im.convert("RGB").resize((40, 40))
    tot = 0.0
    for r, g, b in small.getdata():
        _, _, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        tot += s
    return tot / 1600


# Which template a platform's scans are printed to. This matters: SNES (1.565),
# Genesis (1.598) and N64 (1.574) are within 2% of each other, so the aspect alone
# CANNOT tell them apart — pick by aspect and every SNES box comes out a Genesis
# box, 5mm too thin. The platform knows; ask it.
# Templates whose panels Cover Project stores ROTATED 90 degrees.
#
# A US SNES box and an N64 box are LANDSCAPE — logo across the top, the SUPER NINTENDO
# band along the bottom, the red Nintendo 64 band down the right. Their scans hold each
# panel on its side, so the wrap measures the same ~1.60 either way and NOTHING in the
# geometry can tell you which. An NES box, on a similar aspect, is genuinely PORTRAIT
# with its title bar running up the left edge.
#
# So this is not detectable, it is knowable. I tried: a saturation test and a text-line
# test both get it wrong, and both were confident. They read the sideways Zelda logo on
# the N64 box as the real cover, and they turned MadWorld — a portrait Wii case — into
# landscape. It is a fact about a platform's scans; look at one and write it down.
TEMPLATE_ROT = {"snes": 90, "n64": 90}


PLATFORM_TEMPLATE = {
    "super_nintendo": "snes", "nes": "nes",
    "genesis": "genesis", "sega_cd": "genesis", "sega_32x": "genesis",
    "nintendo_64": "n64",
    "playstation_1": "dvd", "playstation_2": "dvd", "playstation_3": "bluray",
    "gamecube": "dvd", "nintendo_wii": "dvd", "dreamcast": "dvd",
    "xbox": "dvd", "xbox_360": "dvd", "sega_saturn": "dvd",
    "nintendo_switch": "switch",
    "playstation_4": "bluray", "playstation_5": "bluray", "xbox_one": "bluray",
}


def classify(im: Image.Image, expect: str | None = None):
    """Find the template this scan was printed to. Returns (name, template) or None.

    `expect` is the platform's template. If the scan's aspect agrees with it, we
    take it — that settles the SNES/Genesis/N64 ambiguity, which aspect can't.
    If it DISAGREES, we don't force it: Cover Project prints PS1 games onto DVD
    keepcases, and the scan is the ground truth about what box it fits.
    """
    ar = im.width / im.height
    if expect and expect in TEMPLATES:
        if abs(ar / _aspect(TEMPLATES[expect]) - 1) <= TOLERANCE:
            return expect, TEMPLATES[expect]
    best, gap = None, 1e9
    for name, t in TEMPLATES.items():
        d = abs(ar / _aspect(t) - 1)
        if d < gap:
            best, gap = (name, t), d
    if gap > TOLERANCE:
        return None
    return best


def normalize(im: Image.Image):
    """Land the scan the right way up: landscape, back on the left, front on the right."""
    w, h = im.size
    if h > w:
        # Portrait: it's a wrap on its side. Which side is the question — and the
        # answer is different for different scans, so measure rather than assume.
        cw = im.rotate(-90, expand=True)     # clockwise
        third = cw.width // 3
        left = _saturation(cw.crop((0, 0, third, cw.height)))
        right = _saturation(cw.crop((cw.width - third, 0, cw.width, cw.height)))
        if abs(right - left) < MIN_CONFIDENCE:
            return None, "ambiguous rotation"
        # We want the FRONT (the saturated end) on the right.
        im = cw if right > left else im.rotate(90, expand=True)
    return im, None


def slice_wrap(data: bytes, expect: str | None = None):
    """bytes -> {back, spine, front, template, case}. None if it isn't a usable wrap."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im, err = normalize(im)
    if im is None:
        return None, err

    hit = classify(im, expect)
    if not hit:
        return None, f"no template matches aspect {im.width / im.height:.3f}"
    name, (back_mm, spine_mm, front_mm, h_mm) = hit

    total = back_mm + spine_mm + front_mm
    w = im.width
    x1 = round(w * back_mm / total)
    x2 = round(w * (back_mm + spine_mm) / total)

    # Sanity: a front-left scan would put the art on the left. Catch it here too —
    # this is the last line of defence before a case gets built back-to-front.
    lf = _saturation(im.crop((0, 0, x1, im.height)))
    rt = _saturation(im.crop((x2, 0, w, im.height)))
    if lf - rt > MIN_CONFIDENCE * 2:
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
        lf, rt = rt, lf

    return {
        "back":  im.crop((0, 0, x1, im.height)),
        "spine": im.crop((x1, 0, x2, im.height)),
        "front": im.crop((x2, 0, w, im.height)),
        "template": name,
        # The wrap decides the box: this is the case these faces actually fit.
        "case": {"w": front_mm, "h": h_mm, "d": spine_mm},
    }, None
