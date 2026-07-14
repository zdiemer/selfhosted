#!/usr/bin/env python3
"""Pre-render the cartridge shells.

CSS 3D gave us six flat faces per cart, and a cartridge is not six flat faces — it has a chamfered
front, a recessed label well, ribbed flanks and a notched corner, and no amount of clip-path
describes that. So the shells are MODELLED here and rendered to frames, once, offline; the browser
just plays the frames back. Photoreal shells, no runtime 3D, no second rendering system to own.

THE LABEL IS THE WHOLE TRICK. It changes per game, so it cannot be baked into the frames. Instead
each frame is rendered with the label area punched out to transparent, and we export the four
corners of the label quad in image space. At runtime the browser warps that game's art into those
four points (a homography → CSS matrix3d) and slides it UNDER the frame — so the shell's bevel and
ribs draw over the label's edges and occlusion comes for free.

Flat-shaded polygons with a painter's sort, not a z-buffer: a cart is convex enough that sorting by
depth is exact, and it means chamfers and recesses are real geometry rather than shading tricks.

    python3 tools/render_carts.py            # -> static/carts/*.png + *.json
"""

from __future__ import annotations

import json
import math
import pathlib

from PIL import Image, ImageDraw, ImageFilter

OUT = pathlib.Path(__file__).resolve().parent.parent / "static" / "carts"
FRAMES = 24                      # a full turn; 15° a step reads as smooth at this size
SIZE = 420                       # rendered square, then downsampled
SS = 2                           # supersampling
YAW_RANGE = 54                   # ±27° — the object turns, it doesn't spin like a top
TILT = -11                       # a little from above, like it's in your hand
FOV = 620                        # perspective distance in mm

LIGHT = (-0.42, -0.62, 0.66)     # over your left shoulder
AMBIENT = 0.42


# ---------- tiny 3D ----------
def norm(v):
    m = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def rot_y(p, a):
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c + p[2] * s, p[1], -p[0] * s + p[2] * c)


def rot_x(p, a):
    c, s = math.cos(a), math.sin(a)
    return (p[0], p[1] * c - p[2] * s, p[1] * s + p[2] * c)


def project(p, size):
    """Perspective. +z toward the viewer. FOV is in the same (post-scale) units as p."""
    f = FOV * (size / SIZE)          # the camera distance lives in the SAME units as the geometry
    z = f - p[2]
    if z < 1:
        z = 1
    k = f / z
    return (size / 2 + p[0] * k, size / 2 - p[1] * k)


def shade(rgb, n):
    lam = max(0.0, dot(n, LIGHT))
    k = AMBIENT + (1 - AMBIENT) * lam
    return tuple(min(255, int(c * k)) for c in rgb)


# ---------- geometry helpers ----------
def extrude(outline, z0, z1, col_face, col_side, bevel=0.0, bevel_z=0.0):
    """A slab from a 2D outline: back face, side walls, an optional chamfer, and the front face.

    The chamfer is what makes it look injection-moulded rather than laser-cut: the front face is
    inset by `bevel` and pushed to z1, and a ring of quads connects it to the outline at z1-bevel_z.
    """
    faces = []
    n = len(outline)
    # back
    faces.append(([(x, y, z0) for (x, y) in reversed(outline)], col_side))
    front_outline = outline
    zf = z1
    if bevel > 0:
        # Offset each vertex along its own inward edge normals — NOT radially from the centroid.
        # A radial inset on a tall cart pulls the corners in much further than the long edges, so
        # the chamfer visibly fattens at the corners. This keeps it a constant width all the way
        # round, which is what a moulded chamfer is.
        inset = []
        for i in range(n):
            (px, py), (x, y), (nx, ny) = outline[i - 1], outline[i], outline[(i + 1) % n]
            e1 = norm((x - px, y - py, 0))
            e2 = norm((nx - x, ny - y, 0))
            v = norm((-e1[1] - e2[1], e1[0] + e2[0], 0))    # inward normal, CCW winding
            inset.append((x + v[0] * bevel, y + v[1] * bevel))
        zb = z1 - bevel_z
        # side walls up to the chamfer
        for i in range(n):
            a, b = outline[i], outline[(i + 1) % n]
            faces.append(([(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], zb), (a[0], a[1], zb)], col_side))
        # the chamfer ring
        for i in range(n):
            a, b = outline[i], outline[(i + 1) % n]
            ia, ib = inset[i], inset[(i + 1) % n]
            faces.append(([(a[0], a[1], zb), (b[0], b[1], zb), (ib[0], ib[1], z1), (ia[0], ia[1], z1)], col_face))
        front_outline = inset
    else:
        for i in range(n):
            a, b = outline[i], outline[(i + 1) % n]
            faces.append(([(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)], col_side))
    faces.append(([(x, y, zf) for (x, y) in front_outline], col_face))
    return faces


def well(x0, y0, x1, y1, z_face, depth, col):
    """A recessed rectangle in the front face — the label sits at the bottom of it."""
    zb = z_face - depth
    f = []
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        f.append(([(a[0], a[1], z_face), (b[0], b[1], z_face), (b[0], b[1], zb), (a[0], a[1], zb)], col))
    f.append(([(x0, y0, zb), (x1, y0, zb), (x1, y1, zb), (x0, y1, zb)], col))
    return f


def ribs(x0, y0, x1, y1, z, count, col, vertical=True, h=0.9):
    """The moulded anti-slip grips. Real geometry, standing proud of the face."""
    f = []
    if vertical:
        step = (x1 - x0) / count
        for i in range(count):
            a = x0 + i * step
            b = a + step * 0.5
            f.append(([(a, y0, z + h), (b, y0, z + h), (b, y1, z + h), (a, y1, z + h)], col))
    else:
        step = (y1 - y0) / count
        for i in range(count):
            a = y0 + i * step
            b = a + step * 0.5
            f.append(([(x0, a, z + h), (x1, a, z + h), (x1, b, z + h), (x0, b, z + h)], col))
    return f


def rounded(w, h, r, notch=None, steps=5, radii=None):
    """Cart outline, centred, counter-clockwise, with rounded corners.

    `notch` chamfers ONE corner ("tr" = the Game Boy's anti-insert cut). It has to be emitted in
    sequence, not filtered out afterwards — dropping the arc points and appending the chamfer at
    the end of the list produces a polygon that crosses itself, which is exactly what the first
    render did (the NES came out as a folded paper aeroplane).
    """
    hw, hh = w / 2, h / 2
    # Real carts are not uniformly rounded: the SNES and the N64 both have generous TOP corners and
    # much tighter bottom ones, where the shell meets the connector. (tr, tl, bl, br)
    rtr, rtl, rbl, rbr = radii or (r, r, r, r)
    pts = []

    def arc(cx, cy, rr, a0, a1):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))

    # CCW from the right edge: TR, TL, BL, BR
    if notch == "tr":
        n = max(rtr, 10)
        pts.append((hw, hh - n))
        pts.append((hw - n, hh))
    else:
        arc(hw - rtr, hh - rtr, rtr, 0, 90)
    arc(-hw + rtl, hh - rtl, rtl, 90, 180)
    arc(-hw + rbl, -hh + rbl, rbl, 180, 270)
    arc(hw - rbr, -hh + rbr, rbr, 270, 360)
    return pts


# ---------- the carts ----------
# Everything in millimetres, from references. Label rect is in the same space.
def nes():
    W, H, D = 120, 133, 20
    body = rounded(W, H, 4)
    shell = (186, 180, 168)
    side = (150, 145, 135)
    f = extrude(body, -D / 2, D / 2, shell, side, bevel=2.2, bevel_z=2.2)
    lab = (-44, -14, 44, 50)                      # x0,y0,x1,y1
    f += well(lab[0] - 3, lab[1] - 3, lab[2] + 3, lab[3] + 3, D / 2, 1.4, (162, 157, 146))
    return dict(faces=f, label=lab, label_z=D / 2 - 1.4, size=(W, H, D))


def snes():
    """From the photo, not from memory. The first pass had NINE fine vertical ribs down each flank;
    a real SNES cart has THREE chunky horizontal grooves cut into each side wing, the wings stand
    proud of a big recessed label well, and there's a raised lip across the top."""
    W, H, D = 120, 110, 20
    body = rounded(W, H, 0, radii=(13, 13, 4, 4))     # big top corners, tight at the connector
    shell = (189, 185, 176)
    side = (150, 146, 138)
    f = extrude(body, -D / 2, D / 2, shell, side, bevel=2.6, bevel_z=2.6)

    lab = (-38, -30, 38, 34)                          # BIG, and wider than tall
    f += well(lab[0] - 3, lab[1] - 3, lab[2] + 3, lab[3] + 3, D / 2, 2.0, (164, 160, 151))

    # Three grooves per wing. Cut IN, not stuck on.
    for sx in (-1, 1):
        x0, x1 = (-W / 2 + 5, -W / 2 + 16) if sx < 0 else (W / 2 - 16, W / 2 - 5)
        for i in range(3):
            y0 = -20 + i * 13
            f += well(x0, y0, x1, y0 + 7, D / 2 - 2.6, 1.5, (146, 142, 134))
    return dict(faces=f, label=lab, label_z=D / 2 - 2.0, size=(W, H, D))


def n64():
    """From the photo. The defining feature is a WIDE perimeter chamfer ramping down from a raised
    centre plateau — not a flat slab with a bevelled lip — plus very large corner radii."""
    W, H, D = 76, 115, 18
    body = rounded(W, H, 0, radii=(13, 13, 6, 6))
    shell = (68, 70, 77)
    side = (44, 46, 52)
    # The broad ramp: a 6mm chamfer dropping 3mm. This is what reads as "N64 cartridge" at a glance.
    f = extrude(body, -D / 2, D / 2, shell, side, bevel=7.0, bevel_z=2.4)

    lab = (-29, -24, 29, 41)
    f += well(lab[0] - 2.5, lab[1] - 2.5, lab[2] + 2.5, lab[3] + 2.5, D / 2, 1.6, (48, 50, 56))
    # The seam across the lower shell, and the lip above the connector.
    f += well(-W / 2 + 9, -H / 2 + 9, W / 2 - 9, -H / 2 + 12, D / 2, 1.0, (44, 46, 51))
    return dict(faces=f, label=lab, label_z=D / 2 - 1.6, size=(W, H, D))


def gameboy():
    W, H, D = 57, 65.5, 8
    body = rounded(W, H, 3, notch="tr")           # the top-right anti-insert cut
    shell = (202, 198, 188)
    side = (168, 164, 155)
    f = extrude(body, -D / 2, D / 2, shell, side, bevel=1.2, bevel_z=1.2)
    lab = (-21, -12, 21, 22)
    f += well(lab[0] - 1.6, lab[1] - 1.6, lab[2] + 1.6, lab[3] + 1.6, D / 2, 0.7, (178, 174, 165))
    f += ribs(-16, -H / 2 + 3, 16, -H / 2 + 8, D / 2 - 1.2, 9, (176, 172, 163), vertical=True, h=0.5)
    return dict(faces=f, label=lab, label_z=D / 2 - 0.7, size=(W, H, D))


def gba():
    W, H, D = 57, 35, 8
    body = rounded(W, H, 2.5)                     # NO front notch — it's on a bottom REAR corner
    shell = (92, 96, 108)
    side = (70, 74, 84)
    f = extrude(body, -D / 2, D / 2, shell, side, bevel=1.0, bevel_z=1.0)
    lab = (-24, -12, 24, 13)
    f += well(lab[0] - 1.4, lab[1] - 1.4, lab[2] + 1.4, lab[3] + 1.4, D / 2, 0.6, (80, 84, 95))
    return dict(faces=f, label=lab, label_z=D / 2 - 0.6, size=(W, H, D))


def genesis():
    W, H, D = 102, 130, 18
    body = rounded(W, H, 7)
    shell = (40, 42, 47)
    side = (28, 30, 34)
    f = extrude(body, -D / 2, D / 2, shell, side, bevel=2.2, bevel_z=2.2)
    lab = (-36, -30, 36, 34)
    f += well(lab[0] - 3, lab[1] - 3, lab[2] + 3, lab[3] + 3, D / 2, 1.4, (34, 36, 40))
    # the ribbed band across the top of the face
    f += ribs(-W / 2 + 8, H / 2 - 18, W / 2 - 8, H / 2 - 8, D / 2 - 2.2, 14, (58, 60, 66))
    return dict(faces=f, label=lab, label_z=D / 2 - 1.4, size=(W, H, D))


CARTS = {"nes": nes, "snes": snes, "n64": n64, "gb": gameboy, "gba": gba, "genesis": genesis}


# ---------- render ----------
def render(cart, name):
    OUT.mkdir(parents=True, exist_ok=True)
    faces = cart["faces"]
    lx0, ly0, lx1, ly1 = cart["label"]
    lz = cart["label_z"]
    # Two orderings, on purpose. The normal test needs a counter-clockwise winding seen from +z,
    # or the label's normal points backwards and it never registers as facing you (it didn't —
    # every frame came out "label not visible"). The runtime homography needs the corners in
    # TL,TR,BR,BL. Same four points, two jobs.
    quad_ccw = [(lx0, ly0, lz), (lx1, ly0, lz), (lx1, ly1, lz), (lx0, ly1, lz)]
    quad3 = [(lx0, ly1, lz), (lx1, ly1, lz), (lx1, ly0, lz), (lx0, ly0, lz)]   # TL,TR,BR,BL

    # scale so the tallest cart fills the frame consistently
    span = max(cart["size"][0], cart["size"][1])
    scale = (SIZE * SS * 0.80) / span

    meta = {"frames": FRAMES, "size": SIZE, "quads": [], "front": [],
            "aspect": round((lx1 - lx0) / (ly1 - ly0), 4)}
    sheet = Image.new("RGBA", (SIZE * FRAMES, SIZE), (0, 0, 0, 0))

    for fi in range(FRAMES):
        t = fi / FRAMES
        yaw = math.radians(math.sin(t * 2 * math.pi) * (YAW_RANGE / 2))
        tilt = math.radians(TILT)
        big = SIZE * SS
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        def xf(p):
            p = (p[0] * scale, p[1] * scale, p[2] * scale)
            p = rot_y(p, yaw)
            p = rot_x(p, tilt)
            return p

        drawn = []
        for poly, col in faces:
            cam = [xf(p) for p in poly]
            n = norm(cross(
                (cam[1][0] - cam[0][0], cam[1][1] - cam[0][1], cam[1][2] - cam[0][2]),
                (cam[2][0] - cam[0][0], cam[2][1] - cam[0][1], cam[2][2] - cam[0][2])))
            if n[2] <= 0.02:                     # back-facing
                continue
            z = sum(p[2] for p in cam) / len(cam)
            pts = [project(p, big) for p in cam]
            drawn.append((z, pts, shade(col, n)))
        for z, pts, col in sorted(drawn, key=lambda x: x[0]):   # painter's: far first
            d.polygon(pts, fill=col + (255,))

        # PUNCH THE LABEL OUT. The art gets warped into this hole at runtime, so the shell's bevel
        # and ribs — which are drawn OUTSIDE it — end up sitting over the label's edges for free.
        ccw = [xf(p) for p in quad_ccw]
        nrm = norm(cross(
            (ccw[1][0] - ccw[0][0], ccw[1][1] - ccw[0][1], ccw[1][2] - ccw[0][2]),
            (ccw[2][0] - ccw[0][0], ccw[2][1] - ccw[0][1], ccw[2][2] - ccw[0][2])))
        facing = nrm[2] > 0.06
        quad = [project(xf(p), big) for p in quad3]
        if facing:
            hole = Image.new("L", (big, big), 255)
            ImageDraw.Draw(hole).polygon(quad, fill=0)
            img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (big, big), 0), hole))

        img = img.resize((SIZE, SIZE), Image.LANCZOS)
        sheet.paste(img, (fi * SIZE, 0), img)
        meta["quads"].append([[round(x / SS, 2), round(y / SS, 2)] for (x, y) in quad])
        meta["front"].append(facing)

    sheet.save(OUT / f"{name}.png", optimize=True)
    (OUT / f"{name}.json").write_text(json.dumps(meta))
    kb = (OUT / f"{name}.png").stat().st_size // 1024
    print(f"  {name:<8} {FRAMES} frames  {kb} KB  label visible in "
          f"{sum(meta['front'])}/{FRAMES}")


if __name__ == "__main__":
    print(f"rendering {len(CARTS)} shells -> {OUT}")
    for name, fn in CARTS.items():
        render(fn(), name)
    print("done")
