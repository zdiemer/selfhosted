"""Metacritic client (repurposed from zdiemer/GamesMaster MetacriticClient).

Uses Metacritic's Fandom backend JSON API (backend.metacritic.com/composer/…)
with the site's public apiKey. One search request per game returns candidates
with their aggregate critic score; we match on title + platform + year and take
the best. Rate limited to 3/s. Falls back to the sheet's Metacritic Rating when
no confident match is found.
"""

from __future__ import annotations

import logging
import urllib.parse

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.metacritic")

_API_KEY = "1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u"
_BACKEND = "https://backend.metacritic.com/composer/metacritic/pages"
_SITE = "https://www.metacritic.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class MetacriticClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(3)

    @property
    def configured(self):
        return True

    def _search(self, title: str):
        self._limiter.wait()
        resp = requests.get(
            f"{_BACKEND}/search/{urllib.parse.quote(str(title).replace('/', ''))}/web",
            params={"apiKey": _API_KEY, "offset": 0, "limit": 20, "mcoTypeId": 13,
                    "componentName": "search-tabs",
                    "componentDisplayName": "Search+Page+Tab+Filters",
                    "componentType": "FilterConfig"},
            headers={"User-Agent": _UA}, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        data = self._search(title)
        comp = next((c for c in data.get("components", [])
                     if c.get("meta", {}).get("componentName") == "search"), None)
        items = ((comp or {}).get("data") or {}).get("items") or []
        best, best_score = None, -1
        for it in items:
            score = (it.get("criticScoreSummary") or {}).get("score")
            if not score:
                continue
            plats = [p.get("name") for p in it.get("platforms", []) if p.get("name")]
            info = self._validator.validate(game, it.get("title"), plats, [it.get("premiereYear")])
            if not (info.likely_match or (info.matched and not any(plats))):
                continue
            if info.match_score > best_score:
                best_score = info.match_score
                best = {
                    "metascore": score,
                    "metascoreFraction": round(score / 100, 4),
                    "name": it.get("title"),
                    "year": it.get("premiereYear"),
                    "url": f"{_SITE}/game/{it.get('slug')}/" if it.get("slug") else None,
                }
        return best
