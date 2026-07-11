"""HowLongToBeat client (repurposed from zdiemer/GamesMaster HltbClient).

HLTB has no public API and hides the search endpoint behind a per-build version
string embedded in their Next.js bundle, so we scrape that string, then POST to
`/api/locate/<version>`. Fragile by nature — if it breaks, matching just returns
None and the app falls back to the sheet's Estimated Time. Rate limited to 1/s.

Matching reuses the ported MatchValidator (title + platform + year).
"""

from __future__ import annotations

import json
import logging
import re
import threading

import requests
from bs4 import BeautifulSoup

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.hltb")

_BASE = "https://howlongtobeat.com"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _hours(seconds):
    return round(seconds / 3600, 2) if seconds else None


class HltbClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(1)
        self._version = None
        self._lock = threading.Lock()

    @property
    def configured(self):
        return True

    def _headers(self):
        return {"content-type": "application/json", "accept": "*/*",
                "User-Agent": _UA, "referer": _BASE + "/"}

    def _update_version(self):
        """Scrape the current /api/locate version string out of the JS bundle."""
        with self._lock:
            self._limiter.wait()
            main = requests.get(_BASE, headers=self._headers(), timeout=30).text
            soup = BeautifulSoup(main, "html.parser")
            manifest = next(
                (s["src"] for s in soup.find_all("script")
                 if s.has_attr("src") and re.search(r"/_next/static/.*/_buildManifest\.js", s["src"])),
                None,
            )
            if not manifest:
                raise ValueError("HLTB: build manifest not found")
            self._limiter.wait()
            man = requests.get(_BASE + manifest, headers=self._headers(), timeout=30).text
            m = re.search(r'"/submit":\["static/css/.*\.css","(?P<submit>static/chunks/pages/submit-[^\."]*\.js)"\]', man)
            if not m:
                raise ValueError("HLTB: submit chunk not found")
            self._limiter.wait()
            sub = requests.get(f"{_BASE}/_next/{m.group('submit')}", headers=self._headers(), timeout=30).text
            v = re.search(r'"/api/locate/"\.concat\("(?P<a>[^"]*)"\)\.concat\("(?P<b>[^"]*)"\)', sub)
            if not v:
                raise ValueError("HLTB: version string not found")
            self._version = v.group("a") + v.group("b")
            log.info("HLTB: locate version refreshed")

    def _search(self, title: str, dlc: bool = False):
        if not self._version:
            self._update_version()
        body = json.dumps({
            "searchType": "games",
            "searchTerms": [title],
            "searchPage": 1,
            "size": 20,
            "searchOptions": {
                "games": {
                    "userId": 0, "platform": "", "sortCategory": "popular",
                    "rangeCategory": "main", "rangeTime": {"min": None, "max": None},
                    "gameplay": {"perspective": "", "flow": "", "genre": "", "difficulty": ""},
                    "rangeYear": {"min": "", "max": ""}, "modifier": "only_dlc" if dlc else "hide_dlc",
                },
                "users": {"sortCategory": "postcount"}, "lists": {"sortCategory": "follows"},
                "filter": "", "sort": 0, "randomizer": 0,
            },
            "useCache": False,
        })
        self._limiter.wait()
        resp = requests.post(f"{_BASE}/api/locate/{self._version}", data=body, headers=self._headers(), timeout=30)
        if resp.status_code != 200:  # version rotated — refresh once and retry
            self._update_version()
            self._limiter.wait()
            resp = requests.post(f"{_BASE}/api/locate/{self._version}", data=body, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        data = self._search(title)
        for r in (data or {}).get("data", []):
            if not r.get("comp_main"):
                continue
            names = [r.get("game_name")] + [a for a in (r.get("game_alias") or "").split(", ") if a]
            plats = [p for p in (r.get("profile_platform") or "").split(", ") if p]
            years = [r["release_world"]] if r.get("release_world") else []
            info = self._validator.validate(game, names, plats, years)
            if info.likely_match or (info.matched and not any(plats)):
                return self._to_hltb(r)
        return None

    def _to_hltb(self, r):
        main = r.get("comp_main") or 0
        plus = r.get("comp_plus") or 0
        hundred = r.get("comp_100") or 0
        allv = r.get("comp_all") or 0
        best = main or plus or hundred or allv
        return {
            "name": r.get("game_name"),
            "url": f"{_BASE}/game/{r.get('game_id')}",
            "main": _hours(main),
            "mainPlus": _hours(plus),
            "hundred": _hours(hundred),
            "allStyles": _hours(allv),
            "best": _hours(best),
        }
