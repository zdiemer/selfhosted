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
import time

import requests
from PIL import Image

log = logging.getLogger("gamedex.shelf")

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

# Cover Project's print templates, in millimetres: back | spine | front | height.
# Kept in step with tools/cp_wrap.py, which is what chose the template offline.
TEMPLATES = {
    "dvd":     (130, 14, 129, 183),
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
            w = self._wraps.get(key)
            e = enrichment.get(mk) or {}
            if w:
                case, src = w["case"], "wrap"
            else:
                mm = FALLBACK_CASE.get(g.get("platform"), DEFAULT_CASE)
                case = {"w": mm[0], "h": mm[1], "d": mm[2]}
                src = "cover" if e.get("cover") else "blank"
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
                "region": (w or {}).get("region") or "",
                "cover": e.get("cover"),      # IGDB image id, for the fallback front
                "hue": self._hues.get(key, "#6E6E78"),   # the spine when we have no scan
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
