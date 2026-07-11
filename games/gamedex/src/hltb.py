"""HowLongToBeat client.

HLTB guards search behind a per-session anti-bot handshake: GET /api/bleed/init
returns {token, hpKey, hpVal}; the search POST to /api/bleed must echo them as
`x-auth-token` / `x-hp-key` / `x-hp-val` headers AND embed a honeypot body field
`{[hpKey]: hpVal}`. The token is reused across searches and refreshed on a 403.
Rate limited to 1/s. Matching reuses the ported MatchValidator.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.hltb")

_BASE = "https://howlongtobeat.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _hours(seconds):
    return round(seconds / 3600, 2) if seconds else None


class HltbClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(1)
        self._session = None            # {token, hpKey, hpVal}
        self._lock = threading.Lock()

    @property
    def configured(self):
        return True

    def _base_headers(self):
        return {"User-Agent": _UA, "referer": _BASE + "/", "Origin": _BASE}

    def _init_session(self):
        with self._lock:
            self._limiter.wait()
            r = requests.get(f"{_BASE}/api/bleed/init?t={int(time.time() * 1000)}",
                             headers=self._base_headers(), timeout=30)
            r.raise_for_status()
            j = r.json()
            self._session = {"token": j["token"], "hpKey": j["hpKey"], "hpVal": j["hpVal"]}
            log.info("HLTB: search session initialized")

    def _search(self, title: str):
        if not self._session:
            self._init_session()
        return self._do_search(title, retry=True)

    def _do_search(self, title, retry):
        s = self._session
        terms = [w for w in title.split(" ") if w]
        body = {
            "searchType": "games", "searchTerms": terms, "searchPage": 1, "size": 20,
            "searchOptions": {
                "games": {"userId": 0, "platform": "", "sortCategory": "popular",
                          "rangeCategory": "main", "rangeTime": {"min": None, "max": None},
                          "gameplay": {"perspective": "", "flow": "", "genre": "", "difficulty": ""},
                          "rangeYear": {"min": "", "max": ""}, "modifier": ""},
                "users": {"sortCategory": "postcount"}, "lists": {"sortCategory": "follows"},
                "filter": "", "sort": 0, "randomizer": 0},
            "useCache": True,
        }
        body[s["hpKey"]] = s["hpVal"]     # honeypot field
        headers = {**self._base_headers(), "Content-Type": "application/json",
                   "x-auth-token": s["token"], "x-hp-key": s["hpKey"], "x-hp-val": s["hpVal"]}
        self._limiter.wait()
        resp = requests.post(f"{_BASE}/api/bleed", data=json.dumps(body), headers=headers, timeout=30)
        if resp.status_code == 403 and retry:   # token expired — refresh once
            self._init_session()
            return self._do_search(title, retry=False)
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

    def override_from_url(self, title, url):
        """Manual mapping: pick the HLTB result whose game id matches the URL."""
        m = re.search(r"/game/(\d+)", url)
        if not m:
            return None
        gid = m.group(1)
        for r in (self._search(title) or {}).get("data", []):
            if str(r.get("game_id")) == gid:
                return self._to_hltb(r)
        return None

    def _to_hltb(self, r):
        main = r.get("comp_main") or 0
        plus = r.get("comp_plus") or 0
        hundred = r.get("comp_100") or 0
        allv = r.get("comp_all") or 0
        best = main or plus or hundred or allv
        return {
            "name": r.get("game_name"), "url": f"{_BASE}/game/{r.get('game_id')}",
            "main": _hours(main), "mainPlus": _hours(plus), "hundred": _hours(hundred),
            "allStyles": _hours(allv), "best": _hours(best),
        }
