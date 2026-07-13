"""Keitai Wiki — metadata for Japanese feature-phone games (DoJa / i-mode / EZweb).

IGDB knows almost nothing about the keitai era, which is why a single platform — DoJa —
accounts for a quarter of the library's metadata-less games. Keitai Wiki is a MediaWiki
(Miraheze) with a real API and a structured {{Game}} infobox: developer, publisher, genre,
series, release date, and a title screen for games that otherwise have no art at all.

An API, not a scrape: no Cloudflare, no HTML parsing beyond the wikitext of one template.

Content is CC BY-SA 4.0, so every record carries `source` and `url` and the UI credits it.
Be a good citizen of a volunteer wiki: descriptive User-Agent, rate limited, and each game
is looked up exactly once (the enrichment cache never re-asks).
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.keitai")

API = "https://keitaiwiki.com/w/api.php"
WIKI = "https://keitaiwiki.com/wiki/"
_UA = "gamedex/1.0 (personal game collection; +https://github.com/zdiemer)"

# The sheet's names for machines this wiki actually covers. Anything else is skipped
# outright, so the wiki is never asked about a PlayStation game.
PLATFORMS = {
    "doja", "i-mode", "imode", "i-appli", "iappli",
    "ezweb", "ez-web", "s!appli", "sappli", "keitai", "j2me",
}


def _fields(wikitext: str) -> dict:
    """Pull the {{Game|...}} infobox out of a page as a dict.

    Fields are NOT one-per-line — the wiki writes `|Caption=© SEGA|Developer = SEGA` on a
    single line — so split on the pipes that sit at depth 0, outside any nested template
    ({{...}}) or link ([[...]]).
    """
    i = wikitext.find("{{Game")
    if i < 0:
        return {}
    depth, j = 0, i
    while j < len(wikitext):
        if wikitext.startswith("{{", j) or wikitext.startswith("[[", j):
            depth += 1
            j += 2
            continue
        if wikitext.startswith("}}", j) or wikitext.startswith("]]", j):
            depth -= 1
            j += 2
            if depth == 0:
                break
            continue
        j += 1
    body = wikitext[i + len("{{Game"):j - 2]

    parts, buf, depth = [], [], 0
    for k, ch in enumerate(body):
        if body.startswith("{{", k) or body.startswith("[[", k):
            depth += 1
        elif body.startswith("}}", k) or body.startswith("]]", k):
            depth -= 1
        if ch == "|" and depth <= 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))

    out = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _clean(v: str) -> str:
    """Wikitext → plain text: strip links, refs, templates, markup."""
    if not v:
        return ""
    v = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", v)   # [[target|label]] -> label
    v = re.sub(r"\[\[([^\]]*)\]\]", r"\1", v)            # [[page]] -> page
    v = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", v)  # [url label] -> label
    v = re.sub(r"\{\{[^}]*\}\}", " ", v)                 # {{template}}
    v = re.sub(r"<[^>]+>", " ", v)                       # <br>, <ref>
    v = v.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", v).strip()


def _people(v: str) -> list[str]:
    """A Developer/Publisher cell may hold several, comma or slash separated."""
    v = _clean(v)
    if not v:
        return []
    return [p.strip() for p in re.split(r"[,/;]| and ", v) if p.strip()]


class KeitaiClient:
    """Looks a game up on Keitai Wiki. Interface matches fallback.py's sources."""

    def __init__(self, validator: MatchValidator = None):
        self._v = validator or MatchValidator()
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._rl = RateLimiter(3)             # gentle on a volunteer wiki

    @property
    def configured(self) -> bool:
        return True

    @staticmethod
    def serves(platform: str | None) -> bool:
        return (platform or "").strip().lower() in PLATFORMS

    def _api(self, **params):
        params.setdefault("format", "json")
        params.setdefault("formatversion", "2")
        self._rl.wait()
        r = self._s.get(API, params=params, timeout=25)
        r.raise_for_status()
        return r.json()

    def _search(self, title: str) -> list[str]:
        res = self._api(action="query", list="search", srsearch=title, srlimit=5)
        return [h["title"] for h in res.get("query", {}).get("search", [])]

    def _wikitext(self, page: str) -> str:
        # formatversion=2 hands back a plain string; the legacy format wraps it in {"*":…}.
        wt = self._api(action="parse", page=page, prop="wikitext").get("parse", {}).get("wikitext")
        return (wt.get("*") if isinstance(wt, dict) else wt) or ""

    def _image_url(self, filename: str) -> str | None:
        """The title screen. The infobox gives a bare filename; ask for its real URL."""
        if not filename:
            return None
        name = _clean(filename).lstrip("File:").strip()
        if not name:
            return None
        res = self._api(action="query", titles="File:" + name,
                        prop="imageinfo", iiprop="url")
        for pg in res.get("query", {}).get("pages", []) or []:
            for ii in pg.get("imageinfo") or []:
                if ii.get("url"):
                    return ii["url"]
        return None

    def _summary(self, wikitext: str) -> str | None:
        """The lead paragraph — the prose after the infobox, before the first heading."""
        body = wikitext
        i = body.find("}}")
        if i >= 0:
            body = body[i + 2:]
        body = re.split(r"\n==", body)[0]
        for para in body.split("\n"):
            txt = _clean(para)
            if len(txt) > 40:
                return txt[:600]
        return None

    def _record(self, page: str, f: dict, wikitext: str, score: int) -> dict:
        years = re.findall(r"\b(19\d{2}|20\d{2})\b", _clean(f.get("release dates", "")))
        series = _clean(f.get("series", ""))
        genre = _clean(f.get("genre", ""))
        return {
            "source": "Keitai Wiki",
            "name": page,
            "url": WIKI + urllib.parse.quote(page.replace(" ", "_")),
            "coverUrl": self._image_url(f.get("picture", "")),
            "summary": self._summary(wikitext),
            "genres": [genre] if genre else [],
            "developers": _people(f.get("developer", "")),
            "publishers": _people(f.get("publisher", "")),
            "franchises": [series] if series else [],
            "year": int(years[0]) if years else None,
            "confidence": score,
        }

    def match(self, game: ExcelGame):
        """First candidate the validator accepts. Platform is never compared — the wiki
        talks about 'i-mode' where the sheet says 'DoJa' — so the score rests on the
        title, the year, and the publisher/developer, exactly as the IGN source does."""
        for page in self._search(game.title):
            try:
                wt = self._wikitext(page)
            except Exception as exc:
                log.warning("keitai: %r page fetch failed: %s", page, exc)
                continue
            if "[[Category:Games]]" not in wt and "{{Game" not in wt:
                continue                                   # a company/console page, not a game
            f = _fields(wt)
            years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b",
                                                _clean(f.get("release dates", "")))]
            devs = _people(f.get("developer", ""))
            pubs = _people(f.get("publisher", ""))
            series = _clean(f.get("series", ""))
            # Alt titles: the lead usually carries "(Japanese: ...)".
            names = [page]
            jp = re.search(r"\(Japanese:\s*([^)]+)\)", wt)
            if jp:
                names.append(_clean(jp.group(1)))
            info = self._v.validate(game, names, [], years, pubs, devs,
                                    [series] if series else [])
            if info.likely_match or info.matched:
                return self._record(page, f, wt, info.match_score)
        return None
