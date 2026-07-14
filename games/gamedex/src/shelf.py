"""The shelf: the physical games, as objects.

Everything here exists to answer one question — what does this game look like as a
box you could pick up? Three answers, in descending order of truth:

    wrap   a Cover Project scan of the real box. We know the front, the spine and
           the back, because someone photographed them.
    cover  no scan, but IGDB has the front. We make a spine from the art's dominant
           hue and a stand-in back.
    blank  nothing at all (a GP2X Wiz game). A plain case with the title on it.

The wrap scans are 3-6 MB and there are two thousand of them, so we do NOT fetch
them up front. `resolve_covers.py` has already decided WHICH scan and WHICH way up,
offline; this module only fetches a scan when someone actually pulls that game off
the shelf, cuts it into three faces, and keeps the pieces on disk forever after.
"""

from __future__ import annotations

import collections
import colorsys
import io
import json
import logging
import pathlib
import threading
import time

import requests
from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger("gamedex.shelf")

_BLUR = ImageFilter.GaussianBlur(9)


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

# Physical cases, in millimetres, for games with no scan to measure. Used only to
# give the fallback box a believable shape.
FALLBACK_CASE = {
    "Super Nintendo Entertainment System": (191, 133, 33),   # landscape
    "Nintendo Entertainment System": (127, 178, 25),
    "Nintendo 64": (190, 133, 33),      # landscape, like the real box
    "Sega Genesis": (133, 184, 28),
    "Nintendo GameCube": (125, 175, 15),
    "Sega Dreamcast": (140, 190, 15),
    "PlayStation": (142, 125, 10),          # a real jewel case: landscape
    "Sega Saturn": (142, 125, 10),
    "PlayStation 2": (135, 190, 14),
    "Nintendo Wii": (135, 190, 14),
    "Xbox": (135, 190, 14),
    "Xbox 360": (135, 190, 14),
    "Nintendo Wii U": (135, 190, 14),
    "PlayStation 3": (135, 171, 14),
    "PlayStation 4": (135, 171, 14),
    "PlayStation 5": (135, 171, 14),
    "Xbox One": (135, 171, 14),
    "Xbox Series X|S": (135, 171, 14),
    "Nintendo Switch": (105, 170, 11),
    "Nintendo Switch 2": (105, 170, 11),
    "Nintendo 3DS": (122, 137, 12),
    "New Nintendo 3DS": (122, 137, 12),
    "Nintendo DS": (125, 137, 12),
    "PlayStation Vita": (105, 137, 12),
    "PlayStation Portable": (105, 170, 14),
    "Nintendo Game Boy Advance": (92, 133, 22),
    "Nintendo Game Boy": (92, 133, 22),
    "Nintendo Game Boy Color": (92, 133, 22),
    # The sheet mostly uses shorthands, and a platform missing from this table silently
    # gets a generic DVD case — which is how a fallback SNES box came out portrait.
    # A US SNES box and an N64 box are LANDSCAPE (see TEMPLATE_ROT in tools/cp_wrap.py).
    "SNES": (191, 133, 33),
    "NES": (127, 178, 25),
    "Genesis": (133, 184, 28),
    "Game Boy": (92, 133, 22),
    "Game Boy Color": (92, 133, 22),
    "Game Boy Advance": (92, 133, 22),
    "GameCube": (125, 175, 15),
    "Wii": (135, 190, 14),
    "Wii U": (135, 190, 14),
    "Dreamcast": (140, 190, 15),
    "Saturn": (142, 125, 10),
    "PSP": (105, 170, 14),
    "PS Vita": (105, 137, 12),
    "3DO": (142, 125, 10),
}
DEFAULT_CASE = (135, 190, 14)

FACES = ("front", "spine", "back")

# Bump when the CUTTING logic changes, so already-cached faces on the volume are
# thrown away and recut. Without this, a fix to how a box is sliced never reaches a
# box that was cut wrong the first time. (v3: on rotated templates the back turns the
# opposite way from the front, to cancel the 3D mirror on the back face.)
CUT_VERSION = "3"

# Bump when the UPLOAD slicing changes, to re-cut stored uploads from originals.
# (v2: honour EXIF orientation.)
UPLOAD_CUT_VERSION = "2"

# Cover Project's print templates, in millimetres: back | spine | front | height.
# Kept in step with tools/cp_wrap.py, which is what chose the template offline.
TEMPLATES = {
    "dvd":     (130, 14, 129, 183),
    "gc":      (124, 14, 124, 175),
    "snes":    (133, 33, 133, 191),
    "nes":     (127, 25, 127, 178),
    "genesis": (133, 28, 133, 184),
    "n64":     (133, 33, 133, 190),
    "switch":  (105, 11, 105, 170),
    "bluray":  (135, 14, 135, 171),
    "jewel":   (142, 10, 142, 125),
}


def _saturation(im: Image.Image) -> float:
    small = im.convert("RGB").resize((40, 40))
    return sum(colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[2]
               for r, g, b in small.getdata()) / 1600


def _strip(im: Image.Image) -> Image.Image:
    """Land the scan as a horizontal back|spine|front strip.

    Some scans arrive portrait, and the direction to turn them is NOT constant: Super
    Metroid has the front at the top, Hades at the bottom. Turn both the same way and
    one comes out back-to-front. A front is art and a back is text and barcodes, so the
    saturated end is the front — and we want it on the right. This must stay identical
    to tools/resolve_covers.py, which measured the scan with the same rule.
    """
    if im.height <= im.width:
        return im
    cw = im.rotate(-90, expand=True)
    third = cw.width // 3
    left = _saturation(cw.crop((0, 0, third, cw.height)))
    right = _saturation(cw.crop((cw.width - third, 0, cw.width, cw.height)))
    return cw if right >= left else im.rotate(90, expand=True)


def dominant_hue(im: Image.Image) -> str:
    """The spine colour for a game with no scanned spine.

    The MEAN colour of box art is brown. Always. A box is one or two strong colours
    over a lot of dark, and averaging that gives you mud. So take the modal HUE of
    the pixels that actually carry colour, weighted by how colourful they are, and
    throw away the greys and the blacks that would otherwise win on volume alone.
    """
    small = im.convert("RGB").resize((60, 80))
    votes: dict[int, float] = collections.defaultdict(float)
    for r, g, b in small.getdata():
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if s < 0.25 or l < 0.12 or l > 0.92:
            continue
        votes[round(h * 24)] += s
    if not votes:
        return "#6E6E78"
    top = max(votes, key=votes.get)
    r, g, b = colorsys.hls_to_rgb((top % 24) / 24, 0.42, 0.55)
    return "#%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))


# Which template a manually-uploaded WRAP is sliced with, by platform. A user uploads
# an upright wrap (they orient it themselves with the rotate control), so unlike the
# Cover Project scans there is no rotated-template weirdness here — just the slice ratio
# and the case dimensions.
UPLOAD_TEMPLATE = {
    "Nintendo Switch": "switch", "Nintendo Switch 2": "switch",
    "PlayStation 5": "bluray", "PlayStation 4": "bluray", "PlayStation 3": "bluray",
    "Xbox One": "bluray", "Xbox Series X|S": "bluray", "Xbox 360": "bluray",
    "PlayStation 2": "dvd", "Nintendo Wii": "dvd", "Nintendo Wii U": "dvd",
    "Wii": "dvd", "Wii U": "dvd", "Xbox": "dvd", "Sega Dreamcast": "dvd",
    "Dreamcast": "dvd", "PlayStation": "dvd", "Sega Saturn": "dvd", "Saturn": "dvd",
    "Nintendo GameCube": "gc", "GameCube": "gc",
    "Super Nintendo Entertainment System": "snes", "SNES": "snes",
    "Nintendo Entertainment System": "nes", "NES": "nes",
    "Nintendo 64": "n64", "Sega Genesis": "genesis", "Genesis": "genesis",
}
DEFAULT_UPLOAD_TEMPLATE = "bluray"


# Where a fallback front came from, in the order we'd rather have it. GameTDB first: for a
# Nintendo disc it is the actual printed box, region and all.
_FRONT_SOURCES = (
    ("gtdbCover", "GameTDB"), ("coverUrl", None), ("vnCover", "VNDB"),
    ("adbCover", "Arcade DB"), ("vgcCover", "VGChartz"),
)


def _front(e: dict) -> tuple[str, str]:
    """(url, source name) for a real box front when IGDB has no cover for the game.

    Mirrors coverSrc() in app.js, minus the IGDB image id (which the shelf already handles
    separately as `cover`). Every one of these URLs was being fetched and stored already —
    the shelf just never knew how to read anything but an IGDB id, so it drew a grey slab.
    """
    for field, label in _FRONT_SOURCES:
        url = e.get(field)
        if url:
            # coverUrl is whatever primary fallback matched (IGN/Steam/LaunchBox/…); the
            # record's own `source` is the honest name for it.
            return url, (label or e.get("source") or "fallback")
    return "", ""


def _front_url(e: dict) -> str:
    return _front(e)[0]


class Shelf:
    def __init__(self, resolved: dict, cache_dir: pathlib.Path):
        self._wraps = resolved.get("wraps", {})
        self._hues = resolved.get("hues", {})
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        # User uploads live in their OWN directory, so a CUT_VERSION cache-clear (which
        # only sweeps self._dir) never touches art someone chose by hand. They also take
        # priority over everything: a manual upload is the user correcting us.
        self._udir = cache_dir / "uploads"
        self._udir.mkdir(parents=True, exist_ok=True)
        self._umanifest = self._udir / "manifest.json"
        self._uploads = self._load_uploads()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._invalidate_stale_cache()
        self._recut_uploads_if_stale()

    def _recut_uploads_if_stale(self) -> None:
        """When the upload SLICING changes, re-cut every upload from its stored original
        so already-broken art fixes itself on deploy. (v2: honour EXIF orientation — a
        3DS wrap uploaded from a phone was sliced sideways into thin strips.)"""
        stamp = self._udir / ".upload-cut-version"
        if stamp.exists() and stamp.read_text().strip() == UPLOAD_CUT_VERSION:
            return
        n = 0
        for key, meta in list(self._uploads.items()):
            orig = self._udir / f"{key.replace('/', '_')}.orig"
            if not orig.exists():
                continue
            try:
                self.set_cover(key, orig.read_bytes(), kind=meta.get("kind", "wrap"),
                               platform="", rotate=meta.get("rotate", 0),
                               x1=meta.get("x1"), x2=meta.get("x2"), case=meta.get("case"),
                               face_rot=meta.get("faceRot", 0))
                n += 1
            except Exception as e:
                log.warning("re-cut upload %s: %s", key, e)
        stamp.write_text(UPLOAD_CUT_VERSION)
        if n:
            log.info("shelf: re-cut %d uploads (v%s)", n, UPLOAD_CUT_VERSION)

    def _load_uploads(self) -> dict:
        try:
            return json.loads(self._umanifest.read_text())
        except Exception:
            return {}

    def _save_uploads(self) -> None:
        tmp = self._umanifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._uploads))
        tmp.replace(self._umanifest)

    def _invalidate_stale_cache(self) -> None:
        """Drop the whole cut cache when the cutting logic changed, so a box that was
        sliced wrong the first time gets a fresh, correct cut instead of the old one."""
        stamp = self._dir / ".cut-version"
        if stamp.exists() and stamp.read_text().strip() == CUT_VERSION:
            return
        n = 0
        for f in self._dir.glob("*.jpg"):
            f.unlink(missing_ok=True)
            n += 1
        stamp.write_text(CUT_VERSION)
        if n:
            log.info("shelf: cut logic changed (v%s) — cleared %d cached faces", CUT_VERSION, n)

    # ---------- what's on the shelf ----------

    def rows(self, games, enrichment) -> list[dict]:
        """The physical games, in the order a real shelf holds them: grouped by platform,
        alphabetical within each — which is how you'd actually find one."""
        out = []
        for g in games:
            if not g.get("owned"):
                continue
            if (g.get("format") or "").strip().lower() not in ("physical", "both"):
                continue                      # a digital game is not an object
            mk = g.get("_k")
            if not mk:
                continue                      # no match key: nothing to hang art off
            # The BOX is keyed by game AND region: title|platform|year collapses a US and
            # a Japanese copy into one entry, and owning Chrono Trigger on both SNES and
            # Super Famicom then put two Super Famicom boxes on the shelf.
            key = f"{mk}#{(g.get('releaseRegion') or '').strip()}"
            up = self._uploads.get(key)
            w = self._wraps.get(key)
            e = enrichment.get(mk) or {}
            if up:                            # a manual upload wins over everything
                case, src = up["case"], "upload"
            elif w:
                case, src = w["case"], "wrap"
            else:
                mm = FALLBACK_CASE.get(g.get("platform"), DEFAULT_CASE)
                case = {"w": mm[0], "h": mm[1], "d": mm[2]}
                src = "cover" if (e.get("cover") or _front_url(e)) else "blank"
            out.append({
                "k": key,                     # the box (per region)
                "mk": mk,                     # the game, for the detail card
                "t": g.get("title"),
                "p": g.get("platform"),
                "series": g.get("franchise") or "",
                "year": g.get("releaseYear"),
                "done": bool(g.get("completed")),
                "case": case,
                "src": src,
                "region": (up or w or {}).get("region") or "",
                "cover": e.get("cover"),      # IGDB image id, for the fallback front
                # …and a whole URL when IGDB has no art but another source does. A Wii disc
                # IGDB never matched still has a real, region-correct box front on GameTDB's
                # CDN — showing a grey slab instead of it was a choice we were making by
                # accident, because the shelf only ever understood an IGDB image id.
                "coverUrl": _front(e)[0],
                "coverFrom": "IGDB" if e.get("cover") else _front(e)[1],
                "hue": self._hues.get(key, "#6E6E78"),   # the spine when we have no scan
                "uv": (up or {}).get("v"),    # upload version, for cache-busting the faces
                "backReal": (up or w or {}).get("back_is_real", bool(w)),
                # the upload's own settings, so "Change art" can reopen and re-adjust it
                "upload": up and {"kind": up.get("kind"), "rotate": up.get("rotate", 0),
                                  "faceRot": up.get("faceRot", 0),
                                  "x1": up.get("x1"), "x2": up.get("x2"),
                                  "d": up.get("case", {}).get("d")},
            })
        # Sort titles the way a person alphabetises a shelf: ignore a leading article.
        def alpha(t):
            t = (t or "").lower()
            for a in ("the ", "a ", "an "):
                if t.startswith(a):
                    return t[len(a):]
            return t
        out.sort(key=lambda r: (r["p"] or "", alpha(r["t"])))
        return out

    # ---------- the faces ----------

    def warm(self, delay: float = 0) -> None:
        """Cut every wrap we haven't cut yet, in the background, at boot.

        The shelf asks for 165 spines the moment it opens. Cutting them lazily means
        165 cold requests, each of which downloads a 3-6 MB scan first — so the shelf
        paints black rectangles and fills in over the next several minutes. Do it once,
        up front, and every visit afterwards is served off the volume."""
        todo = [k for k in self._wraps
                if not (self._dir / f"{k.replace('/', '_')}.spine.jpg").exists()]
        if not todo:
            log.info("shelf: all %d wraps already cut", len(self._wraps))
            return

        def run():
            if delay:
                time.sleep(delay)              # let the parse + backfills finish first
            # Strictly serial. This runs in the background and nobody is waiting on it,
            # so there is nothing to buy with concurrency except peak memory — and peak
            # memory is exactly what killed the pod. It is also politer to their CDN.
            for n, k in enumerate(todo, 1):
                self.face(k, "spine")          # _cut writes all three faces at once
                if n % 25 == 0:
                    log.info("shelf: cut %d/%d wraps", n, len(todo))
            log.info("shelf: %d wraps cut and cached", len(todo))

        threading.Thread(target=run, name="shelf-warm", daemon=True).start()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def face(self, key: str, face: str) -> bytes | None:
        """One face of one box. Cut from the scan the first time it's asked for, then
        read off disk forever. Two people pulling the same game at once cut it once."""
        if face not in FACES:
            return None
        safe = key.replace("/", "_")
        # A manual upload overrides the auto-resolved cover, so it's checked first.
        if key in self._uploads:
            up = self._udir / f"{safe}.{face}.jpg"
            if up.exists():
                return up.read_bytes()
        if key not in self._wraps:
            return None
        path = self._dir / f"{safe}.{face}.jpg"
        if path.exists():
            return path.read_bytes()

        with self._lock(key):
            if path.exists():                  # someone else did it while we waited
                return path.read_bytes()
            try:
                self._cut(key, safe)
            except Exception as e:
                log.warning("wrap %s: %s", key, e)
                return None
        return path.read_bytes() if path.exists() else None

    # ---------- manual uploads ----------

    def has_upload(self, key: str) -> bool:
        return key in self._uploads

    def uploaded_covers(self) -> dict:
        """{matchKey: {"url": front-face URL, "v": version}} for every manual upload —
        so the grid and drawer can show hand-supplied art as the game's cover, not just
        the shelf. Keyed by match key alone (region dropped); first region wins."""
        from urllib.parse import quote
        out = {}
        for key, up in self._uploads.items():
            mk = key.rsplit("#", 1)[0]
            if mk in out:
                continue
            v = up.get("v", 1)
            # Key in the QUERY string (see api_shelf_face) — a slash in the platform
            # would break a path segment. quote_via keeps it encoded for the value.
            out[mk] = {"url": f"/api/shelf/face?key={quote(key, safe='')}&face=front&v={v}",
                       "v": v}
        return out

    def set_cover(self, key: str, data: bytes, kind: str, platform: str,
                  rotate: int = 0, x1: float | None = None, x2: float | None = None,
                  case: dict | None = None, face_rot: int = 0,
                  crop: dict | None = None) -> dict:
        """Store a user-supplied cover for one game, as three cached faces.

        The shape of the box comes from the IMAGE and the user, not a per-platform table
        (which got a Game Boy box a Blu-ray shape). The editor derives everything and
        passes it in:
          case   — the box's real proportions {w,h,d} in mm; the FRONT face aspect is the
                   uploaded image's own aspect, so nothing is squashed to a template.
          x1,x2  — for a wrap, the spine boundaries as FRACTIONS of width (0..1), which
                   the user drags to line up with their scan. Back is [0,x1], spine
                   [x1,x2], front [x2,1].

        `rotate` (0/90/180/270) is applied to the WHOLE image first, so the user can
        straighten a sideways scan — which is why uploads never need the per-face
        rotation the Cover Project scans do."""
        if kind not in ("wrap", "front"):
            raise ValueError("kind must be 'wrap' or 'front'")
        im = Image.open(io.BytesIO(data))
        # Honour EXIF orientation. A phone photo (or a re-saved scan) can carry an
        # orientation flag that the browser applies automatically — so the editor showed
        # it upright and the guides were placed on that — but PIL does NOT apply it by
        # default, so the server was slicing the raw sideways pixels into thin strips
        # (the "3DS box wrapped vertically" bug). Bake the orientation in, then the
        # server and the editor agree.
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > 2400:                            # sanity cap before any work
            s = 2400 / max(im.size)
            im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        if rotate % 360:
            im = im.rotate(-(rotate % 360), expand=True)   # clockwise, to match the UI

        # Front-only crop, in fractions of the ROTATED image — the editor drags it against
        # what it is showing, which is the post-rotation picture. Cropping here (before the
        # faces are built) means the spine colour is sampled from the art you kept, not from
        # the scanner margin you threw away.
        if kind == "front" and crop:
            cx1 = max(0.0, min(1.0, float(crop.get("x1", 0.0))))
            cy1 = max(0.0, min(1.0, float(crop.get("y1", 0.0))))
            cx2 = max(0.0, min(1.0, float(crop.get("x2", 1.0))))
            cy2 = max(0.0, min(1.0, float(crop.get("y2", 1.0))))
            l, r = sorted((cx1, cx2))
            t, b = sorted((cy1, cy2))
            box = (round(im.width * l), round(im.height * t),
                   round(im.width * r), round(im.height * b))
            if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:   # ignore a degenerate drag
                im = im.crop(box)

        # The case dims: the editor's numbers if given, else a platform fallback so the
        # older callers and the API without a case still work.
        if case and all(k in case for k in ("w", "h", "d")):
            cw, ch, cd = float(case["w"]), float(case["h"]), float(case["d"])
        else:
            mm = FALLBACK_CASE.get(platform, DEFAULT_CASE)
            cw, ch, cd = float(mm[0]), float(mm[1]), float(mm[2])

        if kind == "wrap":
            if x1 is None or x2 is None:              # fall back to a DVD-ish split
                x1, x2 = 130 / 273, 144 / 273
            c1, c2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
            p1, p2 = round(im.width * c1), round(im.width * c2)
            faces = {
                "back": im.crop((0, 0, p1, im.height)),
                "spine": im.crop((p1, 0, p2, im.height)),
                "front": im.crop((p2, 0, im.width, im.height)),
            }
            # SNES/N64 art is a LANDSCAPE strip whose panels are lying on their side, so
            # the faces need the same per-face turn the Cover Project cut does (_cut):
            #   front — rot90, to stand it up.
            #   back  — rot270. The 3D back face is mirrored (rotateY(180)), and a 90°
            #           turn lands on a mirror differently than a 0° one, so the same
            #           rot90 that fixes the front leaves the back upside-down.
            #   spine — 0. It is already thin-and-tall, the shape the spine face wants;
            #           turning it makes a wide sliver that gets stretched across it.
            if face_rot % 360:
                fr = face_rot % 360
                turn = {"front": fr, "back": (fr + 180) % 360, "spine": 0}
                faces = {n: (p.rotate(turn[n], expand=True) if turn[n] else p)
                         for n, p in faces.items()}
        else:
            hue = dominant_hue(im)
            spine = Image.new("RGB", (max(8, round(im.height * cd / ch)), im.height),
                              _hex(hue))
            back = im.filter(_BLUR).point(lambda p: int(p * 0.42))
            faces = {"front": im, "spine": spine, "back": back}

        safe = key.replace("/", "_")
        for name, piece in faces.items():
            long = max(piece.size)
            if long > 600:
                s = 600 / long
                piece = piece.resize((max(1, round(piece.width * s)),
                                      max(1, round(piece.height * s))), Image.LANCZOS)
            tmp = self._udir / f"{safe}.{name}.tmp"
            piece.save(tmp, "JPEG", quality=84, optimize=True)
            tmp.replace(self._udir / f"{safe}.{name}.jpg")

        # Keep the ORIGINAL upload so "Change art" can reopen it and re-adjust — otherwise
        # we'd only have the sliced faces and couldn't re-drag the spine.
        (self._udir / f"{safe}.orig").write_bytes(data)

        # A monotonic version so the browser refetches after a re-upload — the face URL
        # gets ?v=<n>, which changes even though the path is the same (faces are cached
        # immutably otherwise).
        prev = self._uploads.get(key, {}).get("v", 0)
        entry = {"kind": kind, "region": "user",
                 "back_is_real": kind == "wrap", "v": prev + 1,
                 "rotate": rotate % 360, "faceRot": face_rot % 360,
                 "x1": round(float(x1), 4) if x1 is not None else None,
                 "x2": round(float(x2), 4) if x2 is not None else None,
                 # Keep the crop so reopening re-draws the rectangle over the untouched
                 # original, which is what makes it adjustable rather than destructive.
                 "crop": ({k: round(float(crop[k]), 4) for k in ("x1", "y1", "x2", "y2")}
                          if kind == "front" and crop else None),
                 "case": {"w": round(cw, 1), "h": round(ch, 1), "d": round(cd, 1)}}
        with self._guard:
            self._uploads[key] = entry
            self._save_uploads()
        return entry

    def remove_cover(self, key: str) -> bool:
        """Drop a manual upload, reverting the game to its auto-resolved cover."""
        with self._guard:
            if key not in self._uploads:
                return False
            del self._uploads[key]
            self._save_uploads()
        safe = key.replace("/", "_")
        for name in FACES:
            (self._udir / f"{safe}.{name}.jpg").unlink(missing_ok=True)
        (self._udir / f"{safe}.orig").unlink(missing_ok=True)
        return True

    def original(self, key: str) -> tuple[bytes, str] | None:
        """The raw image the user uploaded, so the editor can reopen and re-adjust it."""
        if key not in self._uploads:
            return None
        p = self._udir / f"{key.replace('/', '_')}.orig"
        if not p.exists():
            return None
        data = p.read_bytes()
        ct = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else \
             "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
        return data, ct

    def _cut(self, key: str, safe: str) -> None:
        w = self._wraps[key]
        # We cache OUR OWN slices and never hotlink their CDN. One fetch per game, ever.
        r = requests.get(w["url"], timeout=120, headers={"User-Agent": "gamedex/1.0"})
        r.raise_for_status()

        im = Image.open(io.BytesIO(r.content))
        # These scans are 300-600 dpi. A 3366x2100 JPEG is 6 MB on the wire and TWENTY
        # ONE MEGABYTES decoded — and every crop and rotate copies it again. That OOM-
        # killed the pod at a 512Mi limit. draft() tells libjpeg to decode at a reduced
        # scale in the first place, which is free: we throw the detail away regardless,
        # since no face is ever shown above 600px.
        im.draft("RGB", (1700, 1700))
        im = _strip(im.convert("RGB"))

        back_mm, spine_mm, front_mm, _ = TEMPLATES[w["template"]]
        total = back_mm + spine_mm + front_mm
        x1 = round(im.width * back_mm / total)
        x2 = round(im.width * (back_mm + spine_mm) / total)
        cuts = {
            "back": im.crop((0, 0, x1, im.height)),
            "spine": im.crop((x1, 0, x2, im.height)),
            "front": im.crop((x2, 0, im.width, im.height)),
        }
        # On a rotated-scan platform (SNES, N64) the ART inside each panel is on its
        # side, and the three faces do NOT fix the same way:
        #   spine — already thin-and-tall, the shape the spine face wants. Rotating it
        #     turns it into a wide sliver that gets stretched across the spine.
        #   front — turn it upright: rot90.
        #   back  — turn it the OTHER way: rot270. The back sits on a face that is
        #     mirrored in 3D (rotateY(180)), and a 90-degree turn lands on a mirror
        #     differently than a 0-degree one — so the same rot90 that fixes the front
        #     leaves the back upside-down. The opposite turn cancels the mirror.
        #   (A normal box's back is rot0 and needs no help; its mirror is correct.)
        rot = {"front": w["rot"], "back": (w["rot"] + 180) % 360, "spine": 0} if w["rot"] else {}
        for name, piece in cuts.items():
            if rot.get(name):
                piece = piece.rotate(rot[name], expand=True)
            # 600px on the long edge is more than a 250px case can show, and turns a
            # 6 MB scan into ~40 KB.
            long = max(piece.size)
            if long > 600:
                s = 600 / long
                piece = piece.resize((round(piece.width * s), round(piece.height * s)),
                                     Image.LANCZOS)
            tmp = self._dir / f"{safe}.{name}.tmp"
            piece.save(tmp, "JPEG", quality=82, optimize=True)
            tmp.replace(self._dir / f"{safe}.{name}.jpg")
