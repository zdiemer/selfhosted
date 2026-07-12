"""Co-Optimus — can I actually play this with someone, and how?

IGDB will tell you a game is "co-operative". It will not tell you whether that
means two people on one sofa or eight strangers online, and that is the only part
anyone cares about when deciding what to play tonight. Co-Optimus knows: how many
local players, how many online, splitscreen or not, whether the CAMPAIGN is co-op
or only a side mode, and whether you can drop in halfway through.

The API is an XML document served from a .php endpoint, which is not what anyone
expects from a URL ending in `games.php`:

    GET https://api.co-optimus.com/games.php?search=true&name=Hades&system=4
    -> <games><game><id/><title/><local/><online/><splitscreen/>...</game></games>

Two traps:

1. Cloudflare fronts it and blocks plain `requests` on the TLS fingerprint, not the
   User-Agent — the same wall StrategyWiki puts up. curl_cffi impersonating Chrome
   walks straight through.

2. The search is a SUBSTRING match, so `name=Hades` returns "Outbreak: Shades of
   Horror" (…s-Hades-…). Every result has to be validated against the title,
   platform and year, or you end up telling someone their roguelike is 4-player
   online horror.

Only 13 platforms exist in their system — modern consoles and PC — so everything
else is skipped rather than searched in vain.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import xml.etree.ElementTree as ET

from curl_cffi import requests as crequests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.cooptimus")

API = "https://api.co-optimus.com/games.php"

# Their system ids. The sheet's platform names on the left.
PLATFORMS = {
    "Nintendo Switch": 28,
    "Nintendo Wii U": 20,
    "PC": 4,
    "PlayStation 2": 6,
    "PlayStation 3": 2,
    "PlayStation 4": 22,
    "PlayStation 5": 30,
    "Nintendo Wii": 3,
    "WiiWare": 14,
    "Xbox": 5,
    "Xbox 360": 1,
    "Xbox One": 24,
    "Xbox Series X|S": 31,
}


# The feed is XML carrying HTML entities — the co-op blurbs are full of &nbsp; —
# and ElementTree knows only the five XML ones, so it dies with "undefined entity".
# (GamesMaster sidestepped this by parsing it with an HTML parser.) Resolve every
# entity that isn't one of the five XML ones before handing it to the parser.
_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|apos);)(#?\w+);")


def _document(body: str) -> str:
    """The XML, and only the XML.

    Cloudflare appends its analytics <script> tag AFTER </games>, so the response
    is a valid XML document with junk stapled to the end — which ElementTree
    rejects outright ("junk after document element"). Cut to the document.
    """
    start = body.find("<games>")
    end = body.rfind("</games>")
    if start == -1 or end == -1:
        return ""
    return body[start:end + len("</games>")]


def _sanitize(xml: str) -> str:
    def repl(m):
        ch = htmllib.unescape(f"&{m.group(1)};")
        # Unknown entity: unescape hands it back untouched. Drop it rather than
        # letting it kill the whole document.
        return "" if ch.startswith("&") else ch.replace("&", "&amp;")
    return _ENTITY.sub(repl, xml)


def _int(el, default=0):
    try:
        return int((el.text or "").strip())
    except (AttributeError, ValueError):
        return default


def _text(el):
    return (el.text or "").strip() if el is not None else ""


class CooptimusClient:
    def __init__(self, validator: MatchValidator | None = None):
        self._v = validator or MatchValidator()
        # Their own client rate-limits to 1/sec. Be a guest, not a problem.
        self._limiter = RateLimiter(1)

    def supports(self, platform) -> bool:
        return platform in PLATFORMS

    def match(self, title, platform=None, year=None):
        if platform not in PLATFORMS:
            return None
        self._limiter.wait()
        try:
            r = crequests.get(
                API,
                params={"search": "true", "name": title, "system": str(PLATFORMS[platform])},
                impersonate="chrome",
                timeout=25,
            )
            if r.status_code != 200 or "<game" not in r.text:
                return None
            root = ET.fromstring(_sanitize(_document(r.text)))
        except Exception as e:
            log.debug("cooptimus %s: %s", title, e)
            return None

        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        best = None
        for g in root.findall("game"):
            name = _text(g.find("title"))
            if not name:
                continue
            years = []
            rd = _text(g.find("releasedate"))
            m = re.match(r"^(\d{4})", rd)
            if m:
                years.append(int(m.group(1)))
            pub = _text(g.find("publisher"))
            # The substring search is what makes this necessary: name=Hades returns
            # "Outbreak: Shades of Horror". Validate, or tell someone their roguelike
            # is 4-player online horror.
            info = self._v.validate(game, name, [platform], years or None,
                                    [pub] if pub else None)
            if not info.likely_match:
                continue
            if best is None or info.match_score > best[1].match_score:
                best = (g, info)

        if not best:
            return None
        g, info = best
        local, online = _int(g.find("local")), _int(g.find("online"))
        return {
            "id": _int(g.find("id"), None),
            "name": _text(g.find("title")),
            "url": _text(g.find("url")),
            # The numbers that actually answer the question.
            "localPlayers": local or None,
            "onlinePlayers": online or None,
            "lanPlayers": _int(g.find("lan")) or None,
            "splitscreen": bool(_int(g.find("splitscreen"))),
            "dropIn": bool(_int(g.find("dropindropout"))),
            "campaignCoop": bool(_int(g.find("campaign"))),
            "coopModes": bool(_int(g.find("modes"))),
            "features": [f for f in _text(g.find("featurelist")).split(", ") if f],
            "coopExperience": _text(g.find("coopexp")) or None,
            "confidence": info.match_score,
        }
