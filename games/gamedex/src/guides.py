"""StrategyWiki — walkthroughs and guides.

GameFAQs would be the obvious source and it's Cloudflare-blocked (403). StrategyWiki
is the viable alternative: a MediaWiki, so it has a real API and no bot-blocking.

We link to the guide rather than scraping its contents — a walkthrough belongs on
the site that maintains it, and mirroring it would be both rude and stale.
"""

from __future__ import annotations

import logging

from curl_cffi import requests as curl_requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.guides")

_API = "https://strategywiki.org/w/api.php"
# StrategyWiki 403s a bot-shaped User-Agent even on its public API, so send a
# browser one. (Rate-limited to 2/s, which is well within anyone's tolerance.)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class GuideClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(2)

    @property
    def configured(self):
        return True

    def _get(self, params):
        self._limiter.wait()
        # impersonate=: match a real Chrome TLS handshake. Headers alone don't do
        # it — the block is on the fingerprint, not the User-Agent.
        r = curl_requests.get(_API, params={**params, "format": "json"},
                              headers={"User-Agent": _UA}, timeout=25, impersonate="chrome")
        r.raise_for_status()
        return r.json()

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        j = self._get({"action": "query", "list": "search", "srsearch": title, "srlimit": 5})
        for hit in ((j or {}).get("query") or {}).get("search") or []:
            name = hit.get("title") or ""
            # StrategyWiki subpages ("Hades/Walkthrough") aren't the game's page.
            if "/" in name:
                continue
            if self._validator.validate(game, name).matched:
                return self._to_record(name)
        return None

    def override_from_url(self, title, url):
        page = (url or "").rstrip("/").rsplit("/", 1)[-1].replace("_", " ")
        return self._to_record(page) if page else None

    def _to_record(self, page):
        # Which sub-pages exist tells us what kind of guide it is.
        subs = []
        try:
            j = self._get({"action": "query", "list": "allpages",
                           "apprefix": page + "/", "aplimit": 12})
            subs = [p["title"].split("/", 1)[1]
                    for p in ((j or {}).get("query") or {}).get("allpages") or []
                    if "/" in p.get("title", "")]
        except Exception as exc:
            log.debug("strategywiki subpages for %s: %s", page, exc)
        slug = page.replace(" ", "_")
        return {
            "name": page,
            "url": f"https://strategywiki.org/wiki/{slug}",
            "sections": subs[:8],
            "hasWalkthrough": any("walkthrough" in s.lower() for s in subs),
        }
