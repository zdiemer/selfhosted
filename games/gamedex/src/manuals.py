"""Internet Archive — the instruction booklet.

The thing that was actually in the box, that nobody keeps, and that no games API carries. The
Archive has a `gamemanuals` collection of ~7,500 scans, free and unauthenticated, and its own
BookReader will page through any of them in an iframe — so we don't have to build a PDF viewer,
we just have to find the right item and be sure it IS the right item.

Being sure is the whole job. A plain keyword search on the Archive is worthless here: searching
"Super Mario World manual" returns a Voice of America radio broadcast as its top hit, because it
searches full text across everything ever uploaded. So:

  - constrain to `mediatype:texts`, so it can only be a scanned document
  - require the game's title in the TITLE field, not anywhere in the item
  - require the words manual/instruction/booklet
  - then run the result through MatchValidator like every other source, so a title that merely
    contains ours ("Super Mario World 2") can't win

Even then this is a source that will confidently hand you the wrong booklet if you let it, which
is why the drawer's mapping control takes an override_from_url — paste the right item and it's
pinned.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.manuals")

SEARCH = "https://archive.org/advancedsearch.php"
META = "https://archive.org/metadata/{}"
EMBED = "https://archive.org/embed/{}"
DETAILS = "https://archive.org/details/{}"
_UA = "gamedex/1.0 (personal game collection; +https://github.com/zdiemer)"

# Manuals are a physical-media thing. A game bought on Steam in 2021 never had one, and asking
# about 14,000 of them would be 14,000 requests for nothing.
PLATFORMS = {
    "NES", "Nintendo Entertainment System", "SNES", "Super Nintendo", "Nintendo 64",
    "Nintendo GameCube", "Nintendo Wii", "Nintendo Wii U", "Nintendo DS", "Nintendo 3DS",
    "Game Boy", "Game Boy Color", "Game Boy Advance", "Virtual Boy", "Nintendo Virtual Boy",
    "Sega Genesis", "Sega Master System", "Sega Saturn", "Sega Dreamcast", "Game Gear", "Sega CD",
    "PlayStation", "PlayStation 2", "PlayStation 3", "PlayStation Portable", "PlayStation Vita",
    "Xbox", "Xbox 360", "Atari 2600", "Atari 7800", "Atari Lynx", "Neo-Geo", "TurboGrafx-16",
    "3DO", "WonderSwan", "MSX", "Commodore 64", "Amiga", "ColecoVision", "Intellivision",
}


# What an uploader might call the console in the item's title. Used two ways: stripped out before
# the titles are compared, and counted as evidence that this is the right game's booklet.
PLATFORM_ALIASES = {
    "NES": ("nes", "nintendo entertainment system", "famicom"),
    "SNES": ("snes", "super nintendo", "super famicom", "sfc"),
    "Nintendo 64": ("n64", "nintendo 64"),
    "Nintendo GameCube": ("gamecube", "gcn", "ngc"),
    "Nintendo Wii": ("wii",),
    "Nintendo Wii U": ("wii u", "wiiu"),
    "Nintendo DS": ("nds", "nintendo ds"),
    "Nintendo 3DS": ("3ds",),
    "Game Boy": ("game boy", "gameboy", "gb"),
    "Game Boy Color": ("game boy color", "gbc"),
    "Game Boy Advance": ("game boy advance", "gba"),
    "Sega Genesis": ("genesis", "mega drive", "megadrive"),
    "Sega Master System": ("master system", "sms"),
    "Sega Saturn": ("saturn",),
    "Sega Dreamcast": ("dreamcast", "dc"),
    "Game Gear": ("game gear",),
    "PlayStation": ("playstation", "psx", "ps1", "psone"),
    "PlayStation 2": ("playstation 2", "ps2"),
    "PlayStation 3": ("playstation 3", "ps3"),
    "PlayStation Portable": ("psp",),
    "Xbox": ("xbox",),
    "Xbox 360": ("xbox 360", "x360"),
    "Atari 2600": ("atari 2600", "2600", "vcs"),
    "TurboGrafx-16": ("turbografx", "pc engine"),
    "Neo-Geo": ("neo geo", "neo-geo", "aes", "mvs"),
}
_ALL_ALIASES = sorted({a for v in PLATFORM_ALIASES.values() for a in v}, key=len, reverse=True)

_FURNITURE = re.compile(
    r"\b(manual|instruction[s]?|booklet|scan[s]?|pdf|complete|hq|high\s*quality|"
    r"official|game|guide|insert|box\s*art|cover)\b", re.I)
_REGION_PAREN = re.compile(r"\((?:usa|us|u|eu|europe|jp|japan|j|pal|ntsc|world|en|fr|de)[^)]*\)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _strip_furniture(name: str) -> str:
    s = _REGION_PAREN.sub(" ", name or "")
    s = _FURNITURE.sub(" ", s)
    s = re.sub(r"\(\s*\)", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -–—_")


def _strip_platform(name: str) -> str:
    s = name or ""
    for a in _ALL_ALIASES:
        s = re.sub(rf"\b{re.escape(a)}\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -–—_()")


class ManualClient:
    """Interface matches the other secondary sources in enrich.py."""

    def __init__(self, validator: MatchValidator = None):
        self._v = validator or MatchValidator()
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._rl = RateLimiter(3)              # the Archive is a charity; don't hammer it

    @property
    def configured(self) -> bool:
        return True

    @staticmethod
    def serves(platform: str | None) -> bool:
        return (platform or "") in PLATFORMS

    def _search(self, title: str, rows: int = 6) -> list[dict]:
        # Quote the title so a multi-word game is one phrase, not five loose terms.
        safe = re.sub(r'["\\]', " ", title).strip()
        q = (f'mediatype:texts AND title:("{safe}") '
             f'AND (manual OR instruction OR booklet)')
        params = [("q", q), ("rows", str(rows)), ("output", "json"),
                  ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "year"),
                  ("fl[]", "collection")]
        self._rl.wait()
        r = self._s.get(SEARCH, params=params, timeout=25)
        r.raise_for_status()
        return (r.json().get("response") or {}).get("docs") or []

    def _files(self, identifier: str) -> dict:
        """What the item actually contains — is there a PDF, and how many pages?"""
        self._rl.wait()
        r = self._s.get(META.format(identifier), timeout=25)
        r.raise_for_status()
        j = r.json() or {}
        files = j.get("files") or []
        pdf = next((f["name"] for f in files
                    if str(f.get("name", "")).lower().endswith(".pdf")), None)
        pages = None
        for f in files:
            if f.get("name", "").endswith("_meta.xml"):
                continue
        meta = j.get("metadata") or {}
        return {
            "pdf": pdf,
            "pages": pages,
            "title": meta.get("title"),
            "year": meta.get("year") or meta.get("date"),
            "collections": meta.get("collection") or [],
        }

    def _record(self, doc: dict, score: int) -> dict:
        ident = doc["identifier"]
        return {
            "source": "Internet Archive",
            "identifier": ident,
            "name": doc.get("title"),
            "year": doc.get("year"),
            # BookReader in an iframe: page-turning, zoom and search, for free. Building a PDF
            # viewer to show a scan the Archive already renders would be daft.
            "embed": EMBED.format(ident),
            "url": DETAILS.format(ident),
            "confidence": score,
        }

    def override_from_url(self, title: str, url: str):
        """Manual mapping: paste an archive.org item URL and it's pinned. No validation — you
        picked it, so it's right."""
        m = re.search(r"archive\.org/(?:details|embed)/([^/?#]+)", (url or "").strip())
        if not m:
            return None
        ident = urllib.parse.unquote(m.group(1))
        try:
            info = self._files(ident)
        except Exception:
            return None
        return self._record({"identifier": ident, "title": info.get("title"),
                             "year": info.get("year")}, 15)

    def match_meta(self, meta: dict):
        if not self.serves(meta.get("platform")):
            return None
        return self.match(meta.get("title"), meta.get("platform"), meta.get("year"))

    def match(self, title: str, platform=None, year=None):
        if not title:
            return None
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        try:
            docs = self._search(title)
        except Exception as exc:
            log.warning("manuals: search failed for %r: %s", title, exc)
            return None

        want = _norm(title)
        aliases = PLATFORM_ALIASES.get(platform or "", ())

        # SCORE THEM ALL, THEN TAKE THE BEST — never the first that clears the bar.
        # The Archive's own relevance ranking put "Mega Man 2: The Power Fighters" (an arcade
        # game) above "Mega Man 2 NES Manual", and taking the first acceptable hit meant shipping
        # the wrong booklet. Rank on how well the title actually matches, with a thumb on the
        # scale for an item that names the right console.
        best = None
        for doc in docs:
            name = doc.get("title") or ""
            cleaned = _strip_furniture(name)
            # Platform tokens are stripped before comparing (so "Mega Man 2 NES" can match
            # "Mega Man 2") but their PRESENCE is evidence, so it earns a bonus.
            plat_hit = any(re.search(rf"\b{re.escape(a)}\b", name, re.I) for a in aliases)
            cleaned = _strip_platform(cleaned)

            years = []
            y = doc.get("year")
            if y and re.match(r"^\d{4}$", str(y)):
                years = [int(y)]
            info = self._v.validate(game, [cleaned, name], [], years, [], [], [])
            score = info.match_score or 0
            if _norm(cleaned) == want:
                score += 4                       # the title is EXACTLY ours once the noise is off
            if plat_hit:
                score += 3                       # and it says the right console on the tin
            if best is None or score > best[0]:
                best = (score, doc)

        if not best:
            return None
        score, doc = best
        # A floor, because a weak match here is worse than none: it isn't a wrong number, it's
        # the wrong game's booklet presented as this game's.
        if score < 9:
            log.debug("manuals: best for %r only scored %d (%r) — rejected",
                      title, score, doc.get("title"))
            return None
        return self._record(doc, min(score, 15))
