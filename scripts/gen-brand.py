#!/usr/bin/env python3
"""Draw the brand assets for the three services whose marks this repo owns.

    python3 scripts/gen-brand.py            # all of them
    python3 scripts/gen-brand.py status     # just one

Writes into each service's own directory — nothing here is served from
`scripts/`, this is only where the drawing happens. Re-run after changing a
mark or a palette, then rebuild/redeploy whichever service moved:

    infra/cluster-status/brand/     -> ConfigMap, no image; helm upgrade only
    web/apartment-watch/src/brand/  -> baked into the image; build.sh first
    web/old-diemer-codes/overlay/   -> baked into the image; build.sh first

Each service gets the same six files, so the serving side is the same shape
everywhere and a reader who has seen one has seen all three:

    icon.svg                 the favicon; vector, because 16px
    apple-touch-icon.png     180, iOS, which will not take SVG
    icon-192.png             the small Android/desktop-PWA icon
    icon-512.png             splash + app switcher, uncropped
    icon-maskable-512.png    Android's circle crop; mark pulled into the safe zone
    og.png                   1200x630 link preview (old.diemer.codes: og.jpg,
                             because its card carries a photograph)
    manifest.webmanifest     names the above (old.diemer.codes: manifest.json,
                             which is the filename CRA already links)

`icon.svg` is hand-written next to each mark below and is the *authority* on
the artwork; the drawing calls are a transcription of it. Keeping the favicon
as real vector is worth that duplication — a 16px PNG of any of these marks is
mush — but it does mean a change has to land twice. Each SVG says so.

Why each mark is what it is sits in its own `icon.svg` comment, next to the
artwork it explains.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brand  # noqa: E402
from brand import SS, blend, hex_rgb  # noqa: E402

REPO = brand.REPO

CARD_W, CARD_H = 1200, 630
CARD_SS = 2  # cards supersample less than icons: no hairlines, and 2400x1260 is already 12MP


# ---------------------------------------------------------------------------
# Shared icon scaffolding
# ---------------------------------------------------------------------------
# Every mark is specified in a 64-unit box, exactly like its icon.svg. `draw_at`
# maps that box onto a supersampled tile, optionally shrinking the artwork about
# the centre — which is the entire difference between the normal icon and the
# maskable one.

def icon_canvas(size: int, ground: str, *, maskable: bool = False):
    """(image, draw, unit) for a mark tile. `unit(x, y)` maps 64-box -> pixels."""
    radius = None if maskable else 13.0
    img = brand.tile(size, hex_rgb(ground), radius_units=radius)
    draw = ImageDraw.Draw(img)

    span = 0.70 if maskable else 1.0
    s = size * SS
    k = (s / 64.0) * span
    cx = cy = s / 2.0

    def unit(x: float, y: float) -> tuple[float, float]:
        return (cx + (x - 32.0) * k, cy + (y - 32.0) * k)

    return img, draw, unit, k


def write_icon_set(out_dir: str, svg: str, ground: str, paint) -> None:
    """The six-file set, from one `paint(draw, unit, k)` callable.

    `paint` receives the unit transform rather than pixel coordinates, so the
    artwork is written once at its native 64-unit scale and every size — and
    the maskable inset — falls out of the transform.
    """
    brand.save_text(svg, os.path.join(out_dir, "icon.svg"))

    ground_rgb = hex_rgb(ground)
    for name, size, maskable in [
        ("apple-touch-icon.png", 180, False),
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
    ]:
        img, draw, unit, k = icon_canvas(size, ground, maskable=maskable)
        paint(draw, unit, k)
        # Flattened onto the ground rather than kept transparent: iOS
        # composites a touch icon onto white, so transparent corners come back
        # as white ones bracketing a dark tile.
        brand.save_png(brand.finish(img, size, background=ground_rgb),
                       os.path.join(out_dir, name))


def card_canvas(bg: str):
    """(image, draw, px) for a 1200x630 preview card. `px(v)` scales a design unit."""
    img = Image.new("RGB", (CARD_W * CARD_SS, CARD_H * CARD_SS), hex_rgb(bg))
    return img, ImageDraw.Draw(img), (lambda v: v * CARD_SS)


CARD_MARGIN = 72  # design units; the left margin every card shares


def line(draw, px, x: int, baseline: int, string: str, fnt, fill, *,
         right: int = CARD_W - CARD_MARGIN) -> None:
    """Draw one line of card copy, refusing to draw one that would run off.

    Not a nicety. A preview card is generated once and then only ever looked at
    in someone else's chat client, so an overlong headline silently loses its
    last word and nobody finds out for months. Failing the generator is the
    only moment anyone is watching.
    """
    width = draw.textlength(string, font=fnt)
    if px(x) + width > px(right):
        over = round((px(x) + width - px(right)) / CARD_SS)
        raise SystemExit(
            f"card copy overflows by {over}px at x={x}, baseline={baseline}:\n"
            f"    {string!r}\n"
            f"  shorten it, drop the point size, or split the line.")
    draw.text((px(x), px(baseline)), string, font=fnt, fill=fill, anchor="ls")


def mark_tile(ground: str, paint, size: int) -> Image.Image:
    """A finished RGBA mark tile at `size` px, ready to paste onto a card."""
    img, draw, unit, k = icon_canvas(size, ground, maskable=False)
    paint(draw, unit, k)
    return brand.finish(img, size, background=None)


# ===========================================================================
# status.diemer.codes — "Pulse"
# ===========================================================================
# A liveness spike on the page's own baseline. Two shapes, which is what lets it
# survive 16px, and it draws the health rather than the hardware. Colours are
# lifted straight off the page (:root in the web ConfigMap): zinc ground, and
# the pods/k3s series greens and blues.

STATUS_GROUND = "#09090b"
STATUS_GREEN = "#10b981"   # --pods / --ok
STATUS_BLUE = "#0ea5e9"    # --k3s
STATUS_AMBER = "#f59e0b"   # --system
STATUS_FG = "#fafafa"
STATUS_MUTED = "#a1a1aa"
STATUS_RULE = "#27272a"

STATUS_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img"
     aria-label="status.diemer.codes">
  <!--
    The pulse. A status page answers one question — is anything wrong — so the
    mark is the answer's shape rather than a picture of the machines: a
    liveness spike sitting on the baseline the page already draws, in the
    page's own series colours (green pods, blue k3s).

    Two shapes on a tile, deliberately. Anything with more parts — a rack, a
    ring of eight nodes — is a smudge at 16px, and a favicon that only works at
    180px is a favicon that only works where it doesn't matter.

    White-on-dark-tile rather than currentColor, so this is the same object in
    a light tab, a dark tab, a preview card and on a home screen.

    Transcribed by hand into scripts/gen-brand.py, which draws the PNGs with
    Pillow and has no SVG parser. Change one, change the other.
  -->
  <rect width="64" height="64" rx="13" fill="#09090b"/>
  <g fill="none" stroke-linecap="round" stroke-linejoin="round">
    <path d="M7 34h14" stroke="#0ea5e9" stroke-width="3.5" opacity=".5"/>
    <path d="M43 34h14" stroke="#0ea5e9" stroke-width="3.5" opacity=".5"/>
    <path d="M21 34l7-17 7 30 5.5-13H43" stroke="#10b981" stroke-width="4.6"/>
  </g>
</svg>"""


def status_paint(draw, unit, k) -> None:
    dim_blue = blend(hex_rgb(STATUS_BLUE), hex_rgb(STATUS_GROUND), 0.5)
    brand.stroke(draw, [unit(7, 34), unit(21, 34)], 3.5 * k, dim_blue)
    brand.stroke(draw, [unit(43, 34), unit(57, 34)], 3.5 * k, dim_blue)
    brand.stroke(
        draw,
        [unit(21, 34), unit(28, 17), unit(35, 47), unit(40.5, 34), unit(43, 34)],
        4.6 * k,
        hex_rgb(STATUS_GREEN),
    )


def status_card(path: str) -> None:
    img, draw, px = card_canvas(STATUS_GROUND)

    # A single soft green wash off the top right, so a 1200x630 flat black
    # rectangle isn't what lands in someone's chat client.
    brand.radial_wash(img, hex_rgb(STATUS_GREEN),
                      center=(px(940), px(110)), radius=px(560), strength=0.16)

    tile = mark_tile(STATUS_GROUND, status_paint, px(60))
    img.paste(tile, (px(72), px(62)), tile)

    word = brand.display(px(30))
    x = px(150)
    draw.text((x, px(103)), "status", font=word, fill=hex_rgb(STATUS_FG), anchor="ls")
    x += draw.textlength("status", font=word)
    draw.text((x, px(103)), ".diemer.codes", font=word, fill=hex_rgb("#52525b"), anchor="ls")

    line(draw, px, 72, 248, "Is everything up?", brand.display(px(76)), hex_rgb(STATUS_FG))

    sub = brand.body(px(25))
    line(draw, px, 72, 304, "Live health for the eight-node k3s cluster at home — nodes, pods,",
         sub, hex_rgb(STATUS_MUTED))
    line(draw, px, 72, 340, "storage and every service on top, refreshed every 30 seconds.",
         sub, hex_rgb(STATUS_MUTED))

    draw.rectangle([px(72), px(410), px(1128), px(410) + CARD_SS], fill=hex_rgb(STATUS_RULE))

    # The three series the page actually draws, as its own silhouette. No
    # figures anywhere on this card on purpose: a static card baked with "143
    # pods" is a photograph of one afternoon, and on a page whose whole subject
    # is freshness a stale number is worse than none.
    label = brand.body(px(14))
    series = [
        (72, "PODS", STATUS_GREEN,
         [(72, 540), (108, 528), (144, 534), (180, 508), (216, 516),
          (252, 490), (288, 498), (324, 476), (360, 484), (392, 470)]),
        (424, "K3S CPU", STATUS_BLUE,
         [(424, 512), (460, 534), (496, 500), (532, 528), (568, 494),
          (604, 522), (640, 486), (676, 516), (712, 498), (744, 524)]),
        (776, "SYSTEM LOAD", STATUS_AMBER,
         [(776, 528), (812, 520), (848, 536), (884, 512), (920, 530),
          (956, 506), (992, 526), (1028, 502), (1064, 522), (1096, 508)]),
    ]
    for x0, name, color, points in series:
        brand.text_tracked(draw, (px(x0), px(438)), name, label, hex_rgb(color), tracking=px(1.6))
        brand.stroke(draw, [(px(a), px(b)) for a, b in points], px(3.5), hex_rgb(color))
        draw.rectangle([px(x0), px(560), px(points[-1][0]), px(560) + CARD_SS],
                       fill=hex_rgb("#1c1c20"))

    # Not quantized, unlike the other two. This card's only large area is the
    # green wash falling off across near-black, and a 256-colour palette turns
    # that into visible vertical banding — the one image in the set where the
    # saving costs something you can see.
    brand.save_png(img.resize((CARD_W, CARD_H), Image.LANCZOS), path)


def gen_status() -> None:
    out = os.path.join(REPO, "infra", "cluster-status", "brand")
    print("==> status.diemer.codes")
    write_icon_set(out, STATUS_SVG, STATUS_GROUND, status_paint)
    status_card(os.path.join(out, "og.png"))
    brand.save_text(
        brand.manifest(
            name="Cluster Status",
            short_name="Cluster",
            description="Live health for the k3s cluster — nodes, pods, storage and services.",
            background=STATUS_GROUND,
            theme=STATUS_GROUND,
        ),
        os.path.join(out, "manifest.webmanifest"),
    )


# ===========================================================================
# homes.diemer.codes — "Bay window"
# ===========================================================================
# The Victorian bay: cornice, sill, three panes with the wide flat face in the
# middle, one light on. Specifically San Francisco housing rather than housing
# in general, and the lit pane is the premise — somewhere out there is one worth
# a text. Palette is the app's own (src/web.py :root): deep green ground, and
# the amber it reserves for things that want a decision.

HOMES_GROUND = "#0B6E4F"   # --accent (light)
HOMES_PAPER = "#F7F8FA"    # --paper
HOMES_LIT = "#E9B067"      # --flag (dark) — the one warm thing in the palette
HOMES_INK = "#12161C"      # --ink
HOMES_INK_2 = "#5B6472"    # --fog
HOMES_LINE = "#E2E6EC"     # --line
HOMES_SOFT = "#E6F2ED"     # --accent-soft
HOMES_FLAG = "#8A5200"     # --flag (light)
HOMES_FLAG_SOFT = "#FBF0DF"

HOMES_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img"
     aria-label="homes.diemer.codes">
  <!--
    A San Francisco bay window: cornice above, sill below, three panes with the
    wide flat face in the middle, and one light on.

    A map pin with a house in it would read faster and would also be the icon
    every rental app already has. This one is about a specific city's housing
    stock, and the lit pane is the whole product — the tool is silent unless
    something passes, so the mark is the moment it isn't.

    Green ground and amber pane are the app's own tokens (--accent and --flag
    in src/web.py). Amber is the palette's "this wants a decision" colour,
    which is exactly what a listing that got through the filter is.

    Transcribed by hand into scripts/gen-brand.py, which draws the PNGs with
    Pillow and has no SVG parser. Change one, change the other.
  -->
  <rect width="64" height="64" rx="13" fill="#0B6E4F"/>
  <rect x="12" y="11" width="40" height="5.4" rx="2.2" fill="#F7F8FA"/>
  <rect x="15" y="18" width="34" height="28" rx="2" fill="#F7F8FA"/>
  <rect x="26.1" y="18" width="11.8" height="28" fill="#E9B067"/>
  <g fill="#0B6E4F">
    <rect x="23.6" y="18" width="2.5" height="28"/>
    <rect x="37.9" y="18" width="2.5" height="28"/>
  </g>
  <rect x="12" y="47.6" width="40" height="5.4" rx="2.2" fill="#F7F8FA"/>
</svg>"""


def homes_paint(draw, unit, k) -> None:
    paper = hex_rgb(HOMES_PAPER)
    green = hex_rgb(HOMES_GROUND)

    def box(x0, y0, x1, y1, fill, radius=0.0):
        a, b = unit(x0, y0)
        c, d = unit(x1, y1)
        if radius:
            draw.rounded_rectangle([a, b, c, d], radius=radius * k, fill=fill)
        else:
            draw.rectangle([a, b, c, d], fill=fill)

    box(12, 11, 52, 16.4, paper, radius=2.2)        # cornice
    box(15, 18, 49, 46, paper, radius=2.0)          # window
    box(26.1, 18, 37.9, 46, hex_rgb(HOMES_LIT))     # the lit centre pane
    box(23.6, 18, 26.1, 46, green)                  # mullions
    box(37.9, 18, 40.4, 46, green)
    box(12, 47.6, 52, 53, paper, radius=2.2)        # sill


def homes_card(path: str) -> None:
    img, draw, px = card_canvas(HOMES_PAPER)

    tile = mark_tile(HOMES_GROUND, homes_paint, px(60))
    img.paste(tile, (px(72), px(62)), tile)

    word = brand.display(px(30))
    x = px(150)
    draw.text((x, px(103)), "homes", font=word, fill=hex_rgb(HOMES_INK), anchor="ls")
    x += draw.textlength("homes", font=word)
    draw.text((x, px(103)), ".diemer.codes", font=word, fill=hex_rgb("#8792A2"), anchor="ls")

    headline = brand.display(px(62))
    line(draw, px, 72, 222, "Apartments found in", headline, hex_rgb(HOMES_INK))
    line(draw, px, 72, 288, "the latest sweep.", headline, hex_rgb(HOMES_INK))

    sub = brand.body(px(25))
    line(draw, px, 72, 338, "San Francisco rentals, filtered to what you asked for.",
         sub, hex_rgb(HOMES_INK_2))

    # Three of the app's own cards. The third is dimmed and amber-flagged
    # because the tool's job is rejecting, not listing — a row of three green
    # cards would describe a different product. Figures are illustrative and
    # deliberately unremarkable; nothing here comes from criteria.yaml.
    cards = [
        ("$3,450", "2 bd · Mission", "Rent controlled", False),
        ("$3,900", "2 bd · Bernal", "Parking", False),
        ("$4,600", "2 bd · Noe", "Over budget", True),
    ]
    price_font = brand.display(px(30))
    meta_font = brand.body(px(20))
    chip_font = brand.body(px(16))

    for index, (price, meta, chip, flagged) in enumerate(cards):
        ox = 72 + index * 363
        oy = 396
        photo = hex_rgb("#EDEFF3") if flagged else hex_rgb(HOMES_SOFT)
        bar = hex_rgb("#DCE0E7") if flagged else hex_rgb("#CBE0D7")
        ink = blend(hex_rgb(HOMES_INK), hex_rgb(HOMES_PAPER), 0.55 if flagged else 1.0)
        fog = blend(hex_rgb(HOMES_INK_2), hex_rgb(HOMES_PAPER), 0.55 if flagged else 1.0)

        draw.rounded_rectangle([px(ox), px(oy), px(ox + 330), px(oy + 176)], radius=px(12),
                               fill=hex_rgb("#FFFFFF"), outline=hex_rgb(HOMES_LINE),
                               width=max(1, round(px(1.5))))
        draw.rounded_rectangle([px(ox + 1), px(oy + 1), px(ox + 121), px(oy + 175)],
                               radius=px(11), fill=photo)
        draw.rounded_rectangle([px(ox + 10), px(oy + 120), px(ox + 112), px(oy + 128)],
                               radius=px(4), fill=bar)
        draw.rounded_rectangle([px(ox + 10), px(oy + 136), px(ox + 82), px(oy + 144)],
                               radius=px(4), fill=bar)

        draw.text((px(ox + 146), px(oy + 52)), price, font=price_font, fill=ink, anchor="ls")
        draw.text((px(ox + 146), px(oy + 84)), meta, font=meta_font, fill=fog, anchor="ls")

        chip_w = draw.textlength(chip, font=chip_font) + px(32)
        draw.rounded_rectangle([px(ox + 146), px(oy + 106), px(ox + 146) + chip_w, px(oy + 136)],
                               radius=px(15),
                               fill=hex_rgb(HOMES_FLAG_SOFT) if flagged else hex_rgb(HOMES_SOFT))
        draw.text((px(ox + 162), px(oy + 126)), chip, font=chip_font,
                  fill=hex_rgb(HOMES_FLAG) if flagged else hex_rgb(HOMES_GROUND), anchor="ls")

    brand.save_png(img.resize((CARD_W, CARD_H), Image.LANCZOS), path, quantize=True)


def gen_homes() -> None:
    out = os.path.join(REPO, "web", "apartment-watch", "src", "brand")
    print("==> homes.diemer.codes")
    write_icon_set(out, HOMES_SVG, HOMES_GROUND, homes_paint)
    homes_card(os.path.join(out, "og.png"))
    brand.save_text(
        brand.manifest(
            name="Homes",
            short_name="Homes",
            description="San Francisco rentals, filtered to what you asked for.",
            background=HOMES_PAPER,
            theme=HOMES_GROUND,
        ),
        os.path.join(out, "manifest.webmanifest"),
    )


# ===========================================================================
# old.diemer.codes — "ZD, with a cursor"
# ===========================================================================
# The existing favicon is the monogram and stays the monogram; what it isn't is
# usable on a home screen, being 152KB of black letterforms on transparent that
# vanish on a dark background. This puts the same ZD on the site's own charcoal
# with its own yellow, set in the font the site actually asks for (Fira Code,
# which it has been failing to load since rawgit shut down in 2019), and parks a
# block cursor after it. The page has been sitting there unchanged for six
# years; a cursor is what that looks like.

OLD_GROUND = "#202020"   # Styles.Color.Black
OLD_FG = "#E9E9E9"       # Styles.Color.White
OLD_YELLOW = "#EAC67A"   # Styles.Color.Yellow
OLD_DIM = "#9a9a9a"

OLD_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img"
     aria-label="old.diemer.codes">
  <!--
    NOT the favicon. public/favicon.ico is the site's own 2019 monogram and it
    stays exactly as it is — this is the home-screen icon and the mark on the
    preview card, which is what the .ico could not do: black letterforms on
    transparent disappear on a dark home screen.

    Same ZD, on the site's charcoal with the site's yellow, plus a block cursor.
    site.less asks for Fira Code in three places and has never once loaded it
    (its only @import pointed at cdn.rawgit.com, which shut down in 2019, and
    sat after every rule besides), so setting the monogram in it is the site
    finally getting the face it asked for.

    The glyphs here are <text>, which is fine because this file is NOT served —
    it exists so the mark has a vector source to read. The served artwork is the
    PNG set drawn by scripts/gen-brand.py, which sets the same string in the
    real Fira Code binary. Do not link this from index.html without converting
    the text to paths first; a browser without Fira Code would draw something
    else entirely.
  -->
  <rect width="64" height="64" rx="13" fill="#202020"/>
  <text x="12" y="41" fill="#E9E9E9" font-family="'Fira Code', ui-monospace, monospace"
        font-size="25" font-weight="600" letter-spacing="-0.5">ZD</text>
  <rect x="43" y="20" width="9.5" height="21" rx="1" fill="#EAC67A"/>
</svg>"""


def old_paint(draw, unit, k) -> None:
    # Measured, not placed: the monogram and the cursor are sized off the real
    # font metrics at whatever this tile's scale is, then the pair is centred as
    # one object. Hardcoded coordinates would drift the moment the size changed.
    size = round(25 * k)
    if size < 1:
        return
    face = brand.font("fira-code-600.ttf", size)

    text_w = draw.textlength("ZD", font=face)
    cell = draw.textlength("M", font=face)
    cursor_w = cell * 0.78
    gap = cell * 0.22
    total = text_w + gap + cursor_w

    left = unit(32, 32)[0] - total / 2.0
    baseline = unit(32, 41)[1]

    draw.text((left, baseline), "ZD", font=face, fill=hex_rgb(OLD_FG), anchor="ls")

    # Cursor height from the font's own ascent, so it matches the caps rather
    # than a guess. Sits on the same baseline as the letters.
    ascent, _ = face.getmetrics()
    top = baseline - ascent * 0.80
    draw.rounded_rectangle(
        [left + text_w + gap, top, left + text_w + gap + cursor_w, baseline],
        radius=max(1.0, k * 1.0), fill=hex_rgb(OLD_YELLOW))


def old_card(path: str) -> None:
    img, draw, px = card_canvas(OLD_GROUND)

    # The site's own hero photograph, which BodyContainer::after paints fixed
    # behind the whole page. Reading it out of the frozen submodule rather than
    # keeping a second copy here: it is the same file the site ships.
    photo_path = os.path.join(REPO, "web", "old-diemer-codes", "site",
                              "src", "resources", "images", "sf.jpg")
    if os.path.exists(photo_path):
        band_w, band_h = px(720), px(630)
        photo = Image.open(photo_path).convert("RGB")
        scale = max(band_w / photo.width, band_h / photo.height)
        photo = photo.resize((round(photo.width * scale), round(photo.height * scale)),
                             Image.LANCZOS)
        ox = (photo.width - band_w) // 2
        oy = (photo.height - band_h) // 2
        photo = photo.crop((ox, oy, ox + band_w, oy + band_h))
        img.paste(photo, (px(480), 0))

        # Fade the charcoal back across the photo, so the headline sits on flat
        # colour and the photograph is atmosphere rather than a collage.
        #
        # Built column by column rather than from Image.linear_gradient: the
        # ramp has to be fully opaque well past the band's left edge (or the
        # seam at x=480 shows) and then ease to a fixed floor, and expressing
        # that as a rotate/point chain on a 0-255 ramp was how the first
        # attempt ended up with a visible step exactly where the band starts.
        FADE_FROM, FADE_TO, FLOOR = 470, 1180, 70
        ramp = Image.new("L", (px(CARD_W), 1))
        row = ramp.load()
        for x in range(px(CARD_W)):
            u = x / CARD_SS
            if u <= FADE_FROM:
                row[x, 0] = 255
            elif u >= FADE_TO:
                row[x, 0] = FLOOR
            else:
                t = (u - FADE_FROM) / (FADE_TO - FADE_FROM)
                # Smoothstep, so neither end of the fade has a corner in it.
                t = t * t * (3 - 2 * t)
                row[x, 0] = round(255 - (255 - FLOOR) * t)
        img.paste(Image.new("RGB", img.size, hex_rgb(OLD_GROUND)), (0, 0),
                  ramp.resize((px(CARD_W), px(CARD_H)), Image.NEAREST))
    else:
        print("    WARN: site submodule not checked out; card drawn without the photo")

    tile = mark_tile(OLD_GROUND, old_paint, px(60))
    img.paste(tile, (px(72), px(62)), tile)

    word = brand.display(px(30))
    x = px(150)
    draw.text((x, px(103)), "old", font=word, fill=hex_rgb(OLD_FG), anchor="ls")
    x += draw.textlength("old", font=word)
    draw.text((x, px(103)), ".diemer.codes", font=word, fill=hex_rgb("#7a7a7a"), anchor="ls")

    # The page's own words, not new copy written about it.
    headline = brand.display(px(58))
    line(draw, px, 72, 266, "Software Engineer.", headline, hex_rgb(OLD_FG))
    line(draw, px, 72, 336, "Tinkerer. Lifelong student.", headline, hex_rgb(OLD_FG))

    sub = brand.body(px(25))
    line(draw, px, 72, 396, "Zachary Diemer’s original personal site, kept exactly",
         sub, hex_rgb(OLD_DIM))
    line(draw, px, 72, 432, "as it was — the hosting moved, the page never did.",
         sub, hex_rgb(OLD_DIM))

    # A dashed yellow frame, which is the site's own device: .main-description
    # is a dashed border in this exact yellow.
    tag_font = brand.body(px(17))
    label = "ARCHIVED · 2019"
    tracking = px(2.2)
    width = brand.text_width(draw, label, tag_font, tracking) + px(40)
    _dashed_rect(draw, px(72), px(494), px(72) + width, px(538),
                 hex_rgb(OLD_YELLOW), px(1.5), px(5), px(4))
    brand.text_tracked(draw, (px(92), px(508)), label, tag_font, hex_rgb(OLD_YELLOW), tracking)

    # JPEG, and the only card in the set that is. Full-colour PNG this is 490KB,
    # past where some clients give up on fetching a preview at all; palette
    # PNG is 202KB and comes back with the yellow turned white, because the
    # median cut spends all 256 slots on the photograph. JPEG is simply what a
    # card with a photograph in it wants.
    brand.save_jpeg(img.resize((CARD_W, CARD_H), Image.LANCZOS), path)


def _dashed_rect(draw, x0, y0, x1, y1, color, width, dash, gap) -> None:
    """Pillow has no stroke-dasharray; four dashed runs is the whole of it."""
    def run(ax, ay, bx, by):
        length = max(abs(bx - ax), abs(by - ay))
        step = dash + gap
        position = 0.0
        while position < length:
            end = min(position + dash, length)
            if ax == bx:
                draw.rectangle([ax, ay + position, ax + width, ay + end], fill=color)
            else:
                draw.rectangle([ax + position, ay, ax + end, ay + width], fill=color)
            position += step

    run(x0, y0, x1, y0)
    run(x0, y1, x1, y1)
    run(x0, y0, x0, y1)
    run(x1, y0, x1, y1)


def gen_old() -> None:
    out = os.path.join(REPO, "web", "old-diemer-codes", "overlay")
    print("==> old.diemer.codes")
    brand.save_text(OLD_SVG, os.path.join(out, "icon.svg"))
    ground = hex_rgb(OLD_GROUND)
    for name, size, maskable in [
        ("apple-touch-icon.png", 180, False),
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
    ]:
        image, draw, unit, k = icon_canvas(size, OLD_GROUND, maskable=maskable)
        old_paint(draw, unit, k)
        brand.save_png(brand.finish(image, size, background=ground), os.path.join(out, name))
    old_card(os.path.join(out, "og.jpg"))

    # CRA's own manifest, corrected. The 2019 one declared favicon.ico as a
    # 192x192 image/png, which is wrong twice over and is why saving this to a
    # phone has always produced a letter in a grey circle.
    brand.save_text(
        """{
  "short_name": "Zach Diemer",
  "name": "Zach Diemer's Personal Page",
  "description": "Zachary Diemer's original personal site, archived as it stood in 2019.",
  "icons": [
    { "src": "favicon.ico", "sizes": "192x192", "type": "image/x-icon", "purpose": "any" },
    { "src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" },
    { "src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ],
  "start_url": "./index.html",
  "scope": "./",
  "display": "standalone",
  "theme_color": "#202020",
  "background_color": "#202020"
}""",
        os.path.join(out, "manifest.json"),
    )


# ===========================================================================

TARGETS = {"status": gen_status, "homes": gen_homes, "old": gen_old}


def main() -> None:
    wanted = sys.argv[1:] or list(TARGETS)
    unknown = [w for w in wanted if w not in TARGETS]
    if unknown:
        print(f"unknown target(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(TARGETS)}", file=sys.stderr)
        raise SystemExit(2)
    for name in wanted:
        TARGETS[name]()
    print("\nDone. Rebuild/redeploy whatever moved — see the module docstring.")


if __name__ == "__main__":
    main()
