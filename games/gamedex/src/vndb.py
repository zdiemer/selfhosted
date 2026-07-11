"""VNDB — the visual novel database (api.vndb.org/kana).

An official, documented, key-free JSON API. It knows the 279 visual novels (and
much of the 1,109-strong adventure shelf) far better than IGDB does: a community
rating with a vote count, a *median play length in minutes*, a real synopsis and
a cover.

Gated to VN/adventure genres, mirroring VndbClient.should_skip in GamesMaster.
"""

from __future__ import annotations

import logging
import re

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.vndb")

_URL = "https://api.vndb.org/kana/vn"
_GENRES = {"Visual Novel", "Adventure"}
_FIELDS = ("id, title, alttitle, titles{title, latin}, aliases, released, "
           "rating, votecount, length_minutes, description, image{url}")

# VNDB's BBCode-ish markup, stripped for display.
_BB = re.compile(r"\[/?(?:b|i|u|s|url[^\]]*|spoiler|quote|code|raw)\]", re.I)


class VndbClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(1)      # API allows far more; be polite

    @property
    def configured(self):
        return True

    def _search(self, title, filters=None):
        self._limiter.wait()
        r = requests.post(
            _URL,
            json={"filters": filters or ["search", "=", title], "fields": _FIELDS, "results": 10},
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        r.raise_for_status()
        return (r.json() or {}).get("results") or []

    def match_meta(self, meta):
        if (meta.get("genre") or "") not in _GENRES:
            return None
        return self.match(meta["title"], meta.get("platform"), meta.get("year"))

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        for r in self._search(title):
            names = self._names(r)
            years = []
            if r.get("released") and re.match(r"^\d{4}", str(r["released"])):
                years = [int(str(r["released"])[:4])]
            # No platform check: VNDB's platform codes ("win", "ps4") don't map
            # onto the sheet's names, and a VN's title is distinctive enough.
            info = self._validator.validate(game, names, None, years)
            if info.matched:
                return self._to_record(r)
        return None

    def override_from_url(self, title, url):
        """Manual mapping from a vndb.org/vNNNN url."""
        m = re.search(r"/(v\d+)", url or "")
        if not m:
            return None
        for r in self._search(title, filters=["id", "=", m.group(1)]):
            return self._to_record(r)
        return None

    def _names(self, r):
        names = [r.get("title"), r.get("alttitle")]
        for t in r.get("titles") or []:
            names += [t.get("title"), t.get("latin")]
        names += list(r.get("aliases") or [])
        return [n for n in names if n]

    def _to_record(self, r):
        desc = r.get("description")
        if desc:
            desc = _BB.sub("", desc).strip() or None
        mins = r.get("length_minutes")
        return {
            "name": r.get("title"),
            "url": f"https://vndb.org/{r.get('id')}",
            "rating": round(r["rating"] / 100, 3) if r.get("rating") else None,  # 0–100 → 0–1
            "votes": r.get("votecount"),
            "hours": round(mins / 60, 2) if mins else None,
            "released": r.get("released"),
            "description": desc,
            "cover": ((r.get("image") or {}).get("url")),
        }
