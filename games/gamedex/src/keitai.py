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

import html
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


def _infobox_span(wikitext: str) -> tuple[int, int]:
    """(start, end) of the whole {{Game}} template, brace-balanced.

    Balancing matters: the infobox nests templates ({{I-mode}}) and links, so scanning for
    the first '}}' stops INSIDE it — which is how the lead-paragraph reader ended up
    quoting '|Release Dates=...|Website=...' back at you as the summary.
    """
    i = wikitext.find("{{Game")
    if i < 0:
        return -1, -1
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
                return i, j
            continue
        j += 1
    return i, len(wikitext)


def _fields(wikitext: str) -> dict:
    """Pull the {{Game|...}} infobox out of a page as a dict.

    Fields are NOT one-per-line — the wiki writes `|Caption=© SEGA|Developer = SEGA` on a
    single line — so split on the pipes that sit at depth 0, outside any nested template
    ({{...}}) or link ([[...]]).
    """
    i, j = _infobox_span(wikitext)
    if i < 0:
        return {}
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


def _nihongo(m: re.Match) -> str:
    """{{nihongo|English|日本語|romaji}} -> 'English (日本語)'. Positional args only; the
    template also takes named extras (|lead=yes) that aren't part of the name."""
    args = [a.strip() for a in m.group(1).split("|") if "=" not in a]
    args = [a.replace("'''", "").replace("''", "").strip() for a in args]
    args = [a for a in args if a]
    if not args:
        return " "
    return f"{args[0]} ({args[1]})" if len(args) > 1 else args[0]


def _clean(v: str) -> str:
    """Wikitext → plain prose. Nothing wiki-shaped should reach the reader."""
    if not v:
        return ""
    v = re.sub(r"<!--.*?-->", " ", v, flags=re.S)               # comments
    v = re.sub(r"<ref[^>]*>.*?</ref>", " ", v, flags=re.S)      # footnotes, content and all
    v = re.sub(r"<ref[^>]*/>", " ", v)
    v = re.sub(r"<gallery.*?</gallery>", " ", v, flags=re.S)    # image galleries
    v = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", " ", v, flags=re.I)   # embedded images
    v = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", v)          # [[target|label]] -> label
    v = re.sub(r"\[\[([^\]]*)\]\]", r"\1", v)                   # [[page]] -> page
    v = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", v)        # [url label] -> label
    v = re.sub(r"\[https?://\S+\]", " ", v)                     # bare [url]
    # {{nihongo|Contra|魂斗羅|Kontora}} carries the TITLE of the game. Dropping it with the
    # other templates decapitates the lead sentence ("is a port of the original game...").
    v = re.sub(r"\{\{\s*nihongo\s*\|([^{}]*)\}\}", _nihongo, v, flags=re.I)
    v = re.sub(r"\{\{\s*lang\s*\|[^|{}]*\|([^{}|]*)\}\}", r"\1", v, flags=re.I)
    v = re.sub(r"\{\{[^{}]*\}\}", " ", v)                       # any other {{template}}
    v = re.sub(r"^[*#:;]+", "", v, flags=re.M)                  # list/indent markers
    v = re.sub(r"<[^>]+>", " ", v)                              # <br>, <i>, stray html
    v = v.replace("'''", "").replace("''", "")                  # bold / italic
    v = html.unescape(v)                                        # &amp; &nbsp; &#160;
    v = v.replace(" ", " ")
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

    def _page(self, page: str) -> tuple[str, str]:
        """(real title, wikitext), FOLLOWING redirects — a lot of the titles we search for
        are redirect stubs ('GUNDAM U.C.0079' -> 'MOBILE SUIT GUNDAM U.C.0079', 'Devil May
        Cry: Dante x Vergil' -> the same with a × sign). Fetch the stub and you get no
        infobox and skip a game the wiki actually has."""
        res = self._api(action="parse", page=page, prop="wikitext", redirects=1).get("parse", {})
        wt = res.get("wikitext")
        # formatversion=2 hands back a plain string; the legacy format wraps it in {"*":…}.
        return res.get("title") or page, ((wt.get("*") if isinstance(wt, dict) else wt) or "")

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
        """The lead section as plain prose: everything after the infobox, up to the first
        heading. Skip the infobox by its balanced end, not by the first '}}' — a nested
        {{I-mode}} closes first and the remaining infobox fields read as sentences."""
        _, end = _infobox_span(wikitext)
        body = wikitext[end:] if end > 0 else wikitext
        body = re.split(r"\n\s*={2,}", body)[0]          # stop at the first == Heading ==

        paras = []
        for para in re.split(r"\n\s*\n", body):
            txt = _clean(para)
            if len(txt) < 40 or txt.startswith("Category:"):
                continue
            paras.append(txt)
            if sum(len(p) for p in paras) > 400:
                break
        if not paras:
            return None
        return " ".join(paras)[:800].strip()

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

    def override_from_url(self, title: str, url: str):
        """Manual mapping: take the page straight from a pasted Keitai Wiki URL.

        No validation — you chose this page, so the score is a full manual 15. This is how
        the titles the search can't reach get mapped: the wiki files them under a name the
        sheet doesn't use ('Sonic no Daifūgō' is 'Sonic Daifugo', 'Gundam U.C.0079' is
        'MOBILE SUIT GUNDAM U.C.0079')."""
        m = re.search(r"keitaiwiki\.com/wiki/([^?#]+)", url.strip())
        if not m:
            return None
        page = urllib.parse.unquote(m.group(1)).replace("_", " ")
        page, wt = self._page(page)                       # follows redirects
        if not wt:
            return None
        return self._record(page, _fields(wt), wt, 15)

    def match(self, game: ExcelGame):
        """First candidate the validator accepts. Platform is never compared — the wiki
        talks about 'i-mode' where the sheet says 'DoJa' — so the score rests on the
        title, the year, and the publisher/developer, exactly as the IGN source does."""
        for hit in self._search(game.title):
            try:
                page, wt = self._page(hit)                 # follows redirects
            except Exception as exc:
                log.warning("keitai: %r page fetch failed: %s", hit, exc)
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
