"""VGChartz — sales figures.

Scrapes the games table, which is the only free source of per-title unit sales
we have. VGChartz reports "Total Shipped" for most titles and "Total Sales" for
some; we take whichever is present.

Two caveats worth knowing, since they shape how the numbers should be read:
  * coverage skews hard to major retail releases — most of the collection has no
    entry at all, and that is not a bug;
  * figures are VGChartz's own estimates, not publisher-reported truth.
"""

from __future__ import annotations

import html
import logging
import re

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.vgchartz")

_BASE = "https://www.vgchartz.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Result rows are identified by their striped background — the same hook
# GamesMaster's VgChartzClient uses.
_ROW = re.compile(
    r'<tr style="background-image:url\(\.\./imgs/chartBar(?:_alt)?_large\.gif\); height:70px">(.*?)</tr>',
    re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_GAME_LINK = re.compile(r'href="(/games/game\.php\?id=\d+[^"]*)"')
_ANCHORS = re.compile(r"<a[^>]*>(.*?)</a>", re.S)
_CONSOLE = re.compile(r'<img[^>]*alt="([^"]+)"')
_BOXART = re.compile(r'src="(/games/boxart/[^"]+)"')

# VGChartz console codes → the sheet's platform names (only what we can hit).
_CONSOLES = {
    "PS": "PlayStation", "PS2": "PlayStation 2", "PS3": "PlayStation 3",
    "PS4": "PlayStation 4", "PS5": "PlayStation 5", "PSP": "PlayStation Portable",
    "PSV": "PlayStation Vita", "PSN": "PlayStation Network",
    "XB": "Xbox", "X360": "Xbox 360", "XOne": "Xbox One", "XS": "Xbox Series X|S",
    "NES": "NES", "SNES": "SNES", "N64": "Nintendo 64", "GC": "Nintendo GameCube",
    "Wii": "Nintendo Wii", "WiiU": "Nintendo Wii U", "NS": "Nintendo Switch",
    "NS2": "Nintendo Switch 2", "GB": "Game Boy", "GBA": "Game Boy Advance",
    "DS": "Nintendo DS", "3DS": "Nintendo 3DS", "GEN": "Sega Genesis",
    "DC": "Sega Dreamcast", "SAT": "Sega Saturn", "PC": "PC",
}


def _txt(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _units(s):
    """'14.50m' → 14500000, 'N/A' → None."""
    s = (s or "").strip().lower()
    m = re.match(r"^([\d.]+)\s*([mk])?$", s)
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"m": 1_000_000, "k": 1_000}.get(m.group(2), 1))


class VgChartzClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(1)

    @property
    def configured(self):
        return True

    def _search(self, title):
        self._limiter.wait()
        r = requests.get(
            f"{_BASE}/games/games.php",
            params={"name": title, "results": 20, "showtotalsales": 1, "showshipped": 1,
                    "order": "Sales", "ownership": "Both", "page": 1},
            headers={"User-Agent": _UA}, timeout=30,
        )
        r.raise_for_status()
        return self._parse(r.text)

    def _parse(self, doc):
        out = []
        for m in _ROW.finditer(doc):
            row = m.group(1)
            cells = [_txt(c) for c in _CELL.findall(row)]
            link = _GAME_LINK.search(row)
            # Two anchors per row (box art, then the title); take the one with text.
            name = next((_txt(a) for a in _ANCHORS.findall(row) if _txt(a)), None)
            console = _CONSOLE.search(row)
            boxart = _BOXART.search(row)
            if not name:
                continue
            shipped = _units(cells[-2]) if len(cells) >= 2 else None
            sold = _units(cells[-1]) if cells else None
            out.append({
                "name": name,
                "console": console.group(1) if console else None,
                "platform": _CONSOLES.get(console.group(1)) if console else None,
                "shipped": shipped,
                "sold": sold,
                "url": _BASE + link.group(1) if link else None,
                "boxart": _BASE + boxart.group(1) if boxart else None,
            })
        return out

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        results = self._search(title)
        best = None
        for r in results:
            units = r["shipped"] or r["sold"]
            if not units:
                continue                     # a row with no figure tells us nothing
            info = self._validator.validate(game, r["name"], [r["platform"]] if r["platform"] else None)
            if info.likely_match:
                return self._to_record(r)    # right game AND right platform
            if info.matched and best is None:
                best = r                     # right game, other platform — hold it
        return self._to_record(best) if best else None

    def override_from_url(self, title, url):
        m = re.search(r"[?&]id=(\d+)", url or "")
        if not m:
            return None
        want = m.group(1)
        for r in self._search(title):
            if r["url"] and f"id={want}" in r["url"]:
                return self._to_record(r)
        return None

    def _to_record(self, r):
        if not r:
            return None
        return {
            "name": r["name"], "url": r["url"], "platform": r["platform"],
            "console": r["console"], "boxart": r["boxart"],
            "shipped": r["shipped"], "sold": r["sold"],
            "units": r["shipped"] or r["sold"],
        }
