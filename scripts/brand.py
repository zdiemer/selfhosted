"""Shared drawing plumbing for the brand assets this repo generates.

Three services here own their marks but have no build of their own worth
hanging a rasteriser off: `infra/cluster-status` is two ConfigMaps and stock
nginx, `web/old-diemer-codes` is a frozen 2019 CRA we deliberately do not
modify, and `web/apartment-watch` is a CronJob that happens to also serve a
page. So the PNGs are generated *here*, once, and committed — see
`scripts/gen-brand.py` for the per-service artwork, and each mark's own
`icon.svg` for why it is the shape it is.

(`smite.diemer.codes` is the exception and stays where it is: it already draws
its card live from a snapshot in `src/web/og.py`, which is the right answer for
a page whose subject is freshness. Nothing here touches it.)

Pillow rather than an SVG rasteriser, matching gamedex and whatnow.gg: Pillow
is already in this house's toolbox, and adding cairo/resvg to generate six
files would be a rendering stack in exchange for nothing. The cost is that the
marks are transcribed twice — once as `icon.svg` for real vector crispness at
16px, once as drawing calls here — so any change to a mark has to land in both.
The generator asserts nothing about that; it is a genuine seam, called out in
each `icon.svg`.

Fonts are the same two the other cards use (`scripts/brand-fonts/`), so the
whole set reads as one family in a chat client:

    Archivo 800   display — wordmarks and headlines
    IBM Plex Sans body    — everything else

Everything is drawn at 4x and downsampled. Pillow has no antialiasing on
polygons or thick lines, so supersampling is the only way the diagonal of a
lightning bolt or the shoulder of a rounded tile comes out clean.
"""

from __future__ import annotations

import io
import os
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FONT_DIR = os.path.join(HERE, "brand-fonts")

SS = 4  # supersample factor

RGB = tuple  # (r, g, b)


# --------------------------------------------------------------------- color

def hex_rgb(value: str) -> tuple[int, int, int]:
    """'#0ea5e9' -> (14, 165, 233). Accepts the leading # or not."""
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def blend(fg: Sequence[int], bg: Sequence[int], alpha: float) -> tuple[int, int, int]:
    """Flatten `fg` at `alpha` onto an opaque `bg`.

    The marks are specified in SVG, where the dim strokes carry `opacity`. Here
    everything is drawn opaque onto a known tile colour, so the opacity is
    resolved up front rather than by compositing layers — one code path, and no
    RGBA surface to keep straight.
    """
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))  # type: ignore[return-value]


# --------------------------------------------------------------------- fonts

def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def display(size: int) -> ImageFont.FreeTypeFont:
    return font("archivo-800.ttf", size)


def body(size: int) -> ImageFont.FreeTypeFont:
    return font("plex-sans.ttf", size)


def text_tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    string: str,
    fnt: ImageFont.FreeTypeFont,
    fill: Sequence[int],
    tracking: float = 0.0,
) -> int:
    """Draw `string` with per-character letter-spacing; returns the end x.

    Pillow has no letter-spacing, and the small uppercase labels on these cards
    are unreadable without it — at 14px, `PODS` set solid is a smudge. Drawing
    character by character is the whole trick; kerning is lost, which at label
    sizes and in a screen face nobody will ever see.
    """
    x, y = xy
    for char in string:
        draw.text((x, y), char, font=fnt, fill=tuple(fill))
        x += draw.textlength(char, font=fnt) + tracking
    return round(x)


def text_width(draw: ImageDraw.ImageDraw, string: str, fnt: ImageFont.FreeTypeFont,
               tracking: float = 0.0) -> float:
    if not tracking:
        return draw.textlength(string, font=fnt)
    return sum(draw.textlength(c, font=fnt) for c in string) + tracking * max(0, len(string) - 1)


# -------------------------------------------------------------------- shapes

def stroke(
    draw: ImageDraw.ImageDraw,
    points: Iterable[tuple[float, float]],
    width: float,
    fill: Sequence[int],
) -> None:
    """A polyline with round caps and round joins.

    Pillow's `line(joint="curve")` rounds the *joins* and nothing else, so a
    stroke drawn with it has square ends. Every mark here is specified with
    `stroke-linecap="round"`, and at icon sizes a squared-off cap on a 4.6-unit
    stroke is visibly a different drawing. Discs at each vertex are the fix —
    the same trick the SVG renderer is doing internally.
    """
    pts = [(float(x), float(y)) for x, y in points]
    fill = tuple(fill)
    if len(pts) > 1:
        draw.line(pts, fill=fill, width=round(width), joint="curve")
    r = width / 2.0
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def tile(size: int, color: Sequence[int], radius_units: float | None = 13.0) -> Image.Image:
    """A `size`x`size` icon ground, drawn at 4x for the caller to draw onto.

    `radius_units` is in the mark's own 64-unit box, or None for a square
    bleed. Square is what a *maskable* icon wants: Android applies its own mask
    and a pre-rounded tile gets rounded twice, leaving pale corners inside the
    crop.
    """
    s = size * SS
    img = Image.new("RGB", (s, s), tuple(color))
    if radius_units:
        # Rounded corners are cut, not drawn, so whatever the caller paints
        # afterwards cannot spill past them.
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, s - 1, s - 1], radius=radius_units * (s / 64.0), fill=255)
        img.putalpha(mask)
    return img


def finish(img: Image.Image, size: int, background: Sequence[int] | None = None) -> Image.Image:
    """Downsample a 4x drawing to `size`, flattening any alpha onto `background`."""
    out = img.resize((size, size), Image.LANCZOS)
    if out.mode == "RGBA":
        if background is None:
            return out
        flat = Image.new("RGB", out.size, tuple(background))
        flat.paste(out, mask=out.split()[3])
        return flat
    return out


def radial_wash(
    img: Image.Image,
    color: Sequence[int],
    center: tuple[float, float],
    radius: float,
    strength: float,
) -> None:
    """Paint a soft radial glow onto `img`, in place.

    Pillow's `radial_gradient` is a fixed 256x256 ramp, white in the middle;
    resizing it and using it as a mask is cheaper and smoother than computing
    the falloff per pixel, and the card only needs one.
    """
    d = round(radius * 2)
    ramp = Image.radial_gradient("L").resize((d, d), Image.BICUBIC)
    ramp = ramp.point(lambda v: round((255 - v) * strength))  # invert: bright core
    wash = Image.new("RGB", (d, d), tuple(color))
    img.paste(wash, (round(center[0] - radius), round(center[1] - radius)), ramp)


# --------------------------------------------------------------------- output

def save_png(img: Image.Image, path: str, *, quantize: bool = False) -> None:
    """Write a PNG, making the directory if needed.

    `quantize` matters for the preview cards specifically: they live in
    ConfigMaps and Docker layers, and a 1200x630 flat-colour card drops from
    ~90KB to ~35KB on a 256-colour palette with no visible cost. It is left off
    for anything carrying a photograph.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if quantize:
        img = img.convert("RGB").quantize(colors=256, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)
    img.save(path, format="PNG", optimize=True)
    print(f"    {os.path.relpath(path, REPO):<62} {os.path.getsize(path):>7,} B")


def save_jpeg(img: Image.Image, path: str, quality: int = 84) -> None:
    """Write a JPEG. For the one card that carries a photograph.

    A 1200x630 card of flat colour is a PNG; a card with a photograph in it is
    not. Palette-quantising the photographic one was tried and is a trap — the
    median cut spends all 256 slots on the photo's greys and the single accent
    colour on the card comes back white, which is a worse bug than the file size
    it fixes, and a silent one.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.convert("RGB").save(path, format="JPEG", quality=quality, optimize=True,
                            progressive=True, subsampling=0)
    print(f"    {os.path.relpath(path, REPO):<62} {os.path.getsize(path):>7,} B")


def save_text(content: str, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content.rstrip() + "\n")
    print(f"    {os.path.relpath(path, REPO):<62} {os.path.getsize(path):>7,} B")


def png_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def manifest(
    name: str,
    short_name: str,
    description: str,
    background: str,
    theme: str,
    *,
    start_url: str = "/",
) -> str:
    """The manifest every one of these services gets, with the same icon names.

    `purpose` is split deliberately. A single entry marked `any maskable` tells
    Android the artwork is safe to crop AND safe to show uncropped, which is
    only true of a mark drawn inside the 80% safe circle — and a mark drawn
    that small looks lost in the app switcher, where nothing is cropped. Two
    files, two purposes, each drawn for its own job.
    """
    return f"""{{
  "name": "{name}",
  "short_name": "{short_name}",
  "description": "{description}",
  "start_url": "{start_url}",
  "scope": "{start_url}",
  "display": "standalone",
  "background_color": "{background}",
  "theme_color": "{theme}",
  "icons": [
    {{ "src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" }},
    {{ "src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" }},
    {{ "src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }},
    {{ "src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any" }}
  ]
}}"""
