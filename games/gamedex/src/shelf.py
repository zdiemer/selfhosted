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
import logging
import pathlib
import threading

import requests
from PIL import Image

log = logging.getLogger("gamedex.shelf")

# Physical cases, in millimetres, for games with no scan to measure. Used only to
# give the fallback box a believable shape.
FALLBACK_CASE = {
    "Super Nintendo Entertainment System": (133, 191, 33),
    "Nintendo Entertainment System": (127, 178, 25),
    "Nintendo 64": (133, 190, 33),
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
}
DEFAULT_CASE = (135, 190, 14)

FACES = ("front", "spine", "back")

# Cover Project's print templates, in millimetres: back | spine | front | height.
# Kept in step with tools/cp_wrap.py, which is what chose the template offline.
TEMPLATES = {
    "dvd":     (130, 14, 129, 183),
    "snes":    (133, 33, 133, 191),
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


class Shelf:
    def __init__(self, resolved: dict, cache_dir: pathlib.Path):
        self._wraps = resolved.get("wraps", {})
        self._hues = resolved.get("hues", {})
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---------- what's on the shelf ----------

    def rows(self, games, enrichment) -> list[dict]:
        """The physical games, in the order a shelf would hold them: by platform, then
        by series, then by title — so a run of Zeldas stands together, like a real one."""
        out = []
        for g in games:
            if not g.get("owned"):
                continue
            if (g.get("format") or "").strip().lower() not in ("physical", "both"):
                continue                      # a digital game is not an object
            key = g.get("_k")
            if not key:
                continue                      # no match key: nothing to hang art off
            w = self._wraps.get(key)
            e = enrichment.get(key) or {}
            if w:
                case, src = w["case"], "wrap"
            else:
                mm = FALLBACK_CASE.get(g.get("platform"), DEFAULT_CASE)
                case = {"w": mm[0], "h": mm[1], "d": mm[2]}
                src = "cover" if e.get("cover") else "blank"
            out.append({
                "k": key,
                "t": g.get("title"),
                "p": g.get("platform"),
                "series": g.get("franchise") or "",
                "year": g.get("releaseYear"),
                "done": bool(g.get("completed")),
                "case": case,
                "src": src,
                "region": (w or {}).get("region") or "",
                "cover": e.get("cover"),      # IGDB image id, for the fallback front
                "hue": self._hues.get(key, "#6E6E78"),   # the spine when we have no scan
            })
        out.sort(key=lambda r: (r["p"] or "", r["series"], r["year"] or 0, r["t"] or ""))
        return out

    # ---------- the faces ----------

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def face(self, key: str, face: str) -> bytes | None:
        """One face of one box. Cut from the scan the first time it's asked for, then
        read off disk forever. Two people pulling the same game at once cut it once."""
        if face not in FACES or key not in self._wraps:
            return None
        safe = key.replace("/", "_")
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

    def _cut(self, key: str, safe: str) -> None:
        w = self._wraps[key]
        # We cache OUR OWN slices and never hotlink their CDN. One fetch per game, ever.
        r = requests.get(w["url"], timeout=120, headers={"User-Agent": "gamedex/1.0"})
        r.raise_for_status()
        im = _strip(Image.open(io.BytesIO(r.content)).convert("RGB"))

        back_mm, spine_mm, front_mm, _ = TEMPLATES[w["template"]]
        total = back_mm + spine_mm + front_mm
        x1 = round(im.width * back_mm / total)
        x2 = round(im.width * (back_mm + spine_mm) / total)
        cuts = {
            "back": im.crop((0, 0, x1, im.height)),
            "spine": im.crop((x1, 0, x2, im.height)),
            "front": im.crop((x2, 0, im.width, im.height)),
        }
        for name, piece in cuts.items():
            if w["rot"]:
                piece = piece.rotate(w["rot"], expand=True)
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
