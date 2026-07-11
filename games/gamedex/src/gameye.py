"""GameEye client (repurposed from zdiemer/GamesMaster GameyeClient).

Provides physical-collection market values by condition (Loose / CIB / New …)
for owned physical games. Only queried for games the sheet marks as owned +
physical (the enricher gates it), since it's rate limited to 500/hour. Prices
come back in cents; we return dollars. Matching reuses the ported validator.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.gameye")

_BASE = "https://www.gameye.app/api"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _dollars(cents):
    return round(cents / 100, 2) if cents else None


class GameEyeClient:
    def __init__(self):
        self._v = MatchValidator()
        self._lim = RateLimiter(500 / 3600)     # 500/hour
        self._platforms = None

    @property
    def configured(self):
        return True

    def _platform_map(self):
        if self._platforms is None:
            self._lim.wait()
            d = requests.get(f"{_BASE}/platforms", headers={"User-Agent": _UA}, timeout=30).json()
            self._platforms = {p["id"]: p["name"] for p in d.get("platforms", [])}
        return self._platforms

    def _search(self, title):
        self._lim.wait()
        r = requests.get(f"{_BASE}/deep_search",
                         params={"offset": 0, "limit": 30, "title": title, "order": 0, "asc": 1, "cat": 0},
                         headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
        return r.json().get("records") or []

    def override_from_url(self, title, url):
        """Manual mapping: fetch a GameEye item directly by its encyclopedia id."""
        m = re.search(r"/encyclopedia/(\d+)", url)
        if not m:
            return None
        iid = m.group(1)
        self._lim.wait()
        it = requests.get(f"{_BASE}/items/{iid}", headers={"User-Agent": _UA}, timeout=30).json()
        price = it.get("price") or {}
        if not (price.get("Loose") or price.get("CIB") or price.get("New")):
            return None
        return {
            "source": "gameye", "name": it.get("title"),
            "url": f"https://www.gameye.app/encyclopedia/{iid}",
            "priceLoose": _dollars(price.get("Loose")), "priceCib": _dollars(price.get("CIB")),
            "priceNew": _dollars(price.get("New")), "priceManual": _dollars(price.get("ManualPrice")),
            "priceBox": _dollars(price.get("BoxPrice")),
        }

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        pmap = self._platform_map()
        for rec in self._search(title):
            if rec.get("release_type") != 0:        # standard releases only
                continue
            price = rec.get("price") or {}
            if not any(price.values()):
                continue
            pname = pmap.get(rec.get("platform_id"))
            years = []
            if rec.get("release_date"):
                years = [datetime.fromtimestamp(rec["release_date"], tz=timezone.utc).year]
            info = self._v.validate(game, rec["title"], [pname] if pname else [], years)
            if info.likely_match or (info.matched and not pname):
                return {
                    "source": "gameye", "name": rec["title"],
                    "url": f"https://www.gameye.app/encyclopedia/{rec['id']}",
                    "priceLoose": _dollars(price.get("Loose")),
                    "priceCib": _dollars(price.get("CIB")),
                    "priceNew": _dollars(price.get("New")),
                    "priceManual": _dollars(price.get("ManualPrice")),
                    "priceBox": _dollars(price.get("BoxPrice")),
                }
        return None
