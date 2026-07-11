"""Fallback metadata sources for games IGDB doesn't match.

Tried in order when IGDB returns no match: IGN (cover + genres, keyless),
GameSpot (image + deck summary, needs API key), Steam (header image + summary,
PC only). The first confident match becomes the game's enrichment record, tagged
with `source` so the UI attributes it correctly. Covers come back as full URLs
(field `coverUrl`) rather than IGDB image ids.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.fallback")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class _Ign:
    URL = "https://mollusk.apis.ign.com/graphql"
    HASH = "e1c2e012a21b4a98aaa618ef1b43eb0cafe9136303274a34f5d9ea4f2446e884"

    def __init__(self, validator):
        self._v = validator
        self._lim = RateLimiter(3)

    def _search(self, title):
        self._lim.wait()
        term = urllib.parse.quote(title.replace('"', ""))
        variables = '{"term":"%s","count":20,"objectType":"Game"}' % term
        ext = '{"persistedQuery":{"version":1,"sha256Hash":"%s"}}' % self.HASH
        url = f"{self.URL}?operationName=SearchObjectsByName&variables={variables}&extensions={ext}"
        r = requests.get(url, headers={"User-Agent": _UA, "Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        return (r.json().get("data") or {}).get("searchObjectsByName", {}).get("objects", [])

    def override_from_url(self, title, url):
        """Manual mapping: pick the IGN result whose slug matches the pasted URL."""
        m = re.search(r"ign\.com/games/([^/?#]+)", url)
        if not m:
            return None
        slug = m.group(1)
        for o in self._search(title):
            if o.get("slug") == slug or (o.get("url") or "").rstrip("/").endswith("/" + slug):
                return self._to_record(o)
        return None

    def _to_record(self, o):
        nm = (o.get("metadata") or {}).get("names") or {}
        years = []
        for reg in o.get("objectRegions", []):
            for rel in reg.get("releases", []):
                if rel.get("date"):
                    try:
                        years.append(int(rel["date"][:4]))
                    except ValueError:
                        pass
        return {
            "source": "IGN", "name": nm.get("name") or o.get("slug"),
            "url": ("https://www.ign.com" + o["url"]) if o.get("url") else None,
            "coverUrl": (o.get("primaryImage") or {}).get("url"), "summary": None,
            "genres": [g.get("name") for g in (o.get("genres") or []) if g.get("name")],
            "year": years[0] if years else None, "confidence": 0,
        }

    def match(self, game):
        for o in self._search(game.title):
            names = [o.get("slug")]
            nm = (o.get("metadata") or {}).get("names") or {}
            names += [nm.get("name"), nm.get("short"), *(nm.get("alt") or [])]
            names = [n for n in names if n]
            plats, years = [], []
            for reg in o.get("objectRegions", []):
                for rel in reg.get("releases", []):
                    if rel.get("date"):
                        try:
                            years.append(int(rel["date"][:4]))
                        except ValueError:
                            pass
                    plats += [p.get("name") for p in rel.get("platformAttributes", []) if p.get("name")]
            info = self._v.validate(game, names, plats, years)
            if info.likely_match or (info.matched and not any(plats)):
                return {
                    "source": "IGN", "name": nm.get("name") or o.get("slug"),
                    "url": ("https://www.ign.com" + o["url"]) if o.get("url") else None,
                    "coverUrl": (o.get("primaryImage") or {}).get("url"), "summary": None,
                    "genres": [g.get("name") for g in (o.get("genres") or []) if g.get("name")],
                    "year": years[0] if years else None, "confidence": info.match_score,
                }
        return None


class _GameSpot:
    BASE = "https://www.gamespot.com/api"

    def __init__(self, validator, key):
        self._v = validator
        self._key = key
        self._lim = RateLimiter(200 / 3600)   # GameSpot: 200/hour

    def _games(self, title):
        self._lim.wait()
        r = requests.get(
            f"{self.BASE}/games",
            params={"api_key": self._key, "format": "json", "limit": 20,
                    "field_list": "id,name,deck,image,release_date,site_detail_url,genres",
                    "filter": f"name:{self._v.romanize(title)}"},
            headers={"User-Agent": _UA}, timeout=30,
        )
        r.raise_for_status()
        return r.json().get("results", []) or []

    def match(self, game):
        for res in self._games(game.title):
            year = None
            if res.get("release_date"):
                m = re.search(r"(\d{4})", res["release_date"])
                year = int(m.group(1)) if m else None
            info = self._v.validate(game, res.get("name"), release_years=[year] if year else [])
            if info.exact or (info.matched and info.date_matched):
                img = res.get("image") or {}
                return {
                    "source": "GameSpot", "name": res.get("name"), "url": res.get("site_detail_url"),
                    "coverUrl": img.get("original") or img.get("super_url") or img.get("screen_url"),
                    "summary": res.get("deck"),
                    "genres": [g.get("name") for g in (res.get("genres") or []) if g.get("name")],
                    "year": year, "confidence": info.match_score,
                }
        return None


class _Steam:
    def __init__(self, validator):
        self._v = validator
        self._lim = RateLimiter(2)

    def _appdetails(self, appid):
        self._lim.wait()
        det = requests.get("https://store.steampowered.com/api/appdetails",
                           params={"appids": appid}, headers={"User-Agent": _UA}, timeout=30).json()
        return ((det or {}).get(str(appid)) or {}).get("data") or {}

    def _to_record(self, appid, data):
        m = re.search(r"(\d{4})", (data.get("release_date") or {}).get("date") or "")
        return {
            "source": "Steam", "name": data.get("name"),
            "url": f"https://store.steampowered.com/app/{appid}/",
            "coverUrl": data.get("header_image"), "summary": data.get("short_description"),
            "genres": [g.get("description") for g in (data.get("genres") or []) if g.get("description")],
            "year": int(m.group(1)) if m else None, "confidence": 0,
        }

    def override_from_url(self, title, url):
        """Manual mapping: fetch the Steam app directly by its appid."""
        m = re.search(r"/app/(\d+)", url)
        if not m:
            return None
        data = self._appdetails(m.group(1))
        return self._to_record(m.group(1), data) if data else None

    def match(self, game):
        if not game.platform or game.platform.value != "PC":   # Steam = PC only
            return None
        self._lim.wait()
        s = requests.get("https://store.steampowered.com/api/storesearch/",
                         params={"term": game.title, "cc": "us", "l": "en"},
                         headers={"User-Agent": _UA}, timeout=30).json()
        for it in (s.get("items") or [])[:5]:
            info = self._v.validate(game, it.get("name"))
            if not info.matched:
                continue
            appid = it.get("id")
            self._lim.wait()
            det = requests.get("https://store.steampowered.com/api/appdetails",
                               params={"appids": appid}, headers={"User-Agent": _UA}, timeout=30).json()
            data = ((det or {}).get(str(appid)) or {}).get("data") or {}
            if not data:
                continue
            year = None
            m = re.search(r"(\d{4})", (data.get("release_date") or {}).get("date") or "")
            year = int(m.group(1)) if m else None
            if game.release_year and year and abs(game.release_year - year) > 1:
                continue
            return {
                "source": "Steam", "name": data.get("name"),
                "url": f"https://store.steampowered.com/app/{appid}/",
                "coverUrl": data.get("header_image"), "summary": data.get("short_description"),
                "genres": [g.get("description") for g in (data.get("genres") or []) if g.get("description")],
                "year": year, "confidence": info.match_score,
            }
        return None


class FallbackClient:
    def __init__(self, gamespot_key: str = None, gamespot_enabled: bool = False):
        v = MatchValidator()
        self._clients = {"ign": _Ign(v), "steam": _Steam(v)}
        # GameSpot is OFF by default: its API now 301s to a Cloudflare-protected
        # page (403 "Just a moment…"), so it matches nothing — and at 200 req/hr
        # it stalled the chain ~18s per IGDB miss. Kept for if it ever returns.
        if gamespot_key and gamespot_enabled:
            self._clients["gamespot"] = _GameSpot(v, gamespot_key)
        self._chain = [n for n in ("ign", "gamespot", "steam") if n in self._clients]

    @property
    def configured(self):
        return True

    def client_for(self, name):
        return self._clients.get((name or "").lower())

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        for name in self._chain:
            try:
                res = self._clients[name].match(game)
                if res:
                    return res
            except Exception as exc:
                log.warning("fallback %s failed for %r: %s", name, title, exc)
        return None
