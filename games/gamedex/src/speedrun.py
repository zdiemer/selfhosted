"""speedrun.com — world-record times.

A nice counterweight to HowLongToBeat: HLTB says Hades takes 21 hours, the world
record is 2 minutes 13 seconds.

Public REST API, no key. Two calls per game: search by name, then fetch the top
run of its primary category. Fuzzy title match, so expect misses — a game with no
speedrun community simply isn't there, which is itself information.
"""

from __future__ import annotations

import logging

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.speedrun")

_BASE = "https://www.speedrun.com/api/v1"
_UA = "gamedex/1.0 (personal collection browser)"


def _hms(seconds):
    if seconds is None:
        return None
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


class SpeedrunClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(1)      # the API asks for ~100/min; stay well under

    @property
    def configured(self):
        return True

    def _get(self, path, params=None):
        self._limiter.wait()
        r = requests.get(f"{_BASE}{path}", params=params,
                         headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=25)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        j = self._get("/games", {"name": title, "max": 5})
        for g in (j or {}).get("data") or []:
            names = [g["names"].get("international"), g["names"].get("japanese")]
            names += list((g.get("abbreviation") and [g["abbreviation"]]) or [])
            info = self._validator.validate(game, [n for n in names if n])
            if info.matched:
                return self._records(g)
        return None

    def override_from_url(self, title, url):
        """Manual mapping from a speedrun.com/<abbrev> url."""
        slug = (url or "").rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            return None
        j = self._get(f"/games/{slug}")
        return self._records((j or {}).get("data")) if j else None

    def _records(self, g):
        if not g:
            return None
        recs = self._get(f"/games/{g['id']}/records", {"top": 1, "max": 3, "embed": "category"})
        cats = []
        for lb in (recs or {}).get("data") or []:
            runs = lb.get("runs") or []
            if not runs:
                continue
            run = runs[0]["run"]
            cat = ((lb.get("category") or {}).get("data") or {}).get("name")
            t = (run.get("times") or {}).get("primary_t")
            if not cat or not t:
                continue
            cats.append({"category": cat, "seconds": t, "time": _hms(t), "url": run.get("weblink")})
        if not cats:
            return None
        best = min(cats, key=lambda c: c["seconds"])
        return {
            "name": g["names"].get("international"),
            "url": g.get("weblink"),
            "wrCategory": best["category"],
            "wrTime": best["time"],
            "wrSeconds": best["seconds"],
            "wrUrl": best["url"],
            "categories": cats[:3],
        }
