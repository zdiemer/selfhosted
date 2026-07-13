"""Fallback metadata sources for games IGDB doesn't match.

Tried in order when IGDB returns no match: IGN (cover + genres, keyless),
GameSpot (image + deck summary, needs API key), Steam (header image + summary,
PC only), LaunchBox (box scans + overview, keyless — deepest retro catalogue, so
it goes last and catches what the others miss). The first confident match becomes
the game's enrichment record, tagged with `source` so the UI attributes it
correctly. Covers come back as full URLs (field `coverUrl`) rather than IGDB
image ids.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from keitai import KeitaiClient
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
            "year": int(m.group(1)) if m else None,
            "stores": {"steam": {"id": str(appid), "url": f"https://store.steampowered.com/app/{appid}/"}},
            "confidence": 0,
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
                "year": year,
                "stores": {"steam": {"id": str(appid), "url": f"https://store.steampowered.com/app/{appid}/"}},
                "confidence": info.match_score,
            }
        return None


class _LaunchBox:
    """LaunchBox Games Database — a deep, keyless retro catalogue.

    The site is a Nuxt app now, so the game record isn't in the HTML: it's in the
    __NUXT_DATA__ hydration payload, which is an index-addressed array (objects
    hold *indices* into it rather than values). _deref walks that back into plain
    data. Grubby, but it's a well-formed record rather than scraped markup.
    """

    SEARCH = "https://gamesdb.launchbox-app.com/games/results"
    IMAGES = "https://images.launchbox-app.com"
    NUXT = re.compile(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
    LINK = re.compile(r'href="(/games/details/[^"]+)"')
    # Front-of-box first; fall back to other cover-ish art, never a screenshot.
    COVER_ORDER = ("Box - Front", "Box - Front - Reconstructed", "Fanart - Box - Front",
                   "Cart - Front", "Clear Logo")

    def __init__(self, validator):
        self._v = validator
        self._lim = RateLimiter(1)

    def _get(self, url):
        self._lim.wait()
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return r.text

    def _payload(self, doc):
        m = self.NUXT.search(doc)
        if not m:
            return None
        data = json.loads(m.group(1))

        def deref(v, depth=0):
            if depth > 5 or not isinstance(v, int) or not 0 <= v < len(data):
                return v
            t = data[v]
            if isinstance(t, list):
                return [deref(x, depth + 1) for x in t]
            if isinstance(t, dict):
                return {k: deref(x, depth + 1) for k, x in t.items()}
            return t

        game = next((x for x in data if isinstance(x, dict) and "gameImages" in x and "name" in x), None)
        return {k: deref(v) for k, v in game.items()} if game else None

    def _cover(self, images):
        by_type = {}
        for img in images or []:
            if isinstance(img, dict) and img.get("imageFileName"):
                by_type.setdefault(img["imageTypeName"], img["imageFileName"])
        for want in self.COVER_ORDER:
            if want in by_type:
                return f"{self.IMAGES}/{by_type[want]}"
        return None

    def _to_record(self, g, url, confidence=0):
        names = [n.get("name") for n in (g.get("gameGenres") or []) if isinstance(n, dict)]
        rating = g.get("communityRating")
        return {
            "source": "LaunchBox", "name": g.get("name"), "url": url,
            "coverUrl": self._cover(g.get("gameImages")),
            "summary": (g.get("overview") or None),
            "genres": names,
            "year": int(str(g["releaseDate"])[:4]) if str(g.get("releaseDate") or "")[:4].isdigit() else None,
            "userRating": round(float(rating) / 5, 3) if rating else None,   # 0–5 → 0–1
            "stores": ({"steam": {"id": str(g["steamAppId"]),
                                  "url": f"https://store.steampowered.com/app/{g['steamAppId']}/"}}
                       if g.get("steamAppId") else {}),
            "confidence": confidence,
        }

    def override_from_url(self, title, url):
        g = self._payload(self._get(url))
        return self._to_record(g, url) if g else None

    def match(self, game):
        doc = self._get(f"{self.SEARCH}/{urllib.parse.quote(game.title.replace('/', '').replace(':', ''))}")
        seen = []
        for href in self.LINK.findall(doc):
            if href not in seen:
                seen.append(href)

        # Rank, don't race. LaunchBox indexes rom hacks and fan edits alongside
        # the real thing ("Chrono Trigger+" outranks Chrono Trigger by position),
        # and they validate as likely matches. Score every candidate and prefer an
        # exact title over a merely similar one.
        best = None
        for href in seen[:8]:
            url = f"https://gamesdb.launchbox-app.com{href}"
            try:
                g = self._payload(self._get(url))
            except Exception as exc:
                log.debug("launchbox: %s failed: %s", url, exc)
                continue
            if not g:
                continue
            names = [n for n in (
                [g.get("name")] +
                [a.get("name") for a in (g.get("gameAlternateNames") or []) if isinstance(a, dict)]
            ) if n]
            platform = (g.get("platform") or {}).get("name")
            year = str(g.get("releaseDate") or "")[:4]
            info = self._v.validate(
                game, names, [platform] if platform else None,
                [int(year)] if year.isdigit() else None,
            )
            if not info.likely_match:
                continue
            exact = any(self._v.titles_equal_normalized(game.title, n) for n in names)
            rank = (1 if exact else 0, info.match_score)
            if best is None or rank > best[0]:
                best = (rank, g, url, info.match_score)
        return self._to_record(best[1], best[2], best[3]) if best else None


class FallbackClient:
    def __init__(self, gamespot_key: str = None, gamespot_enabled: bool = False):
        v = MatchValidator()
        self._clients = {"ign": _Ign(v), "steam": _Steam(v), "launchbox": _LaunchBox(v),
                         # Japanese feature phones (DoJa/i-mode). Only ever asked about
                         # those platforms, where it is the ONLY source that knows anything.
                         "keitai": KeitaiClient(v)}
        # GameSpot is OFF by default: its API now 301s to a Cloudflare-protected
        # page (403 "Just a moment…"), so it matches nothing — and at 200 req/hr
        # it stalled the chain ~18s per IGDB miss. Kept for if it ever returns.
        if gamespot_key and gamespot_enabled:
            self._clients["gamespot"] = _GameSpot(v, gamespot_key)
        self._chain = [n for n in ("ign", "gamespot", "steam", "launchbox") if n in self._clients]

    @property
    def configured(self):
        return True

    def client_for(self, name):
        return self._clients.get((name or "").lower())

    def match(self, title, platform=None, year=None):
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        # Keitai Wiki is the only source that knows the Japanese feature phones, and the
        # only one worth asking about them — so for DoJa it goes FIRST, and for everything
        # else it isn't asked at all.
        chain = list(self._chain)
        if KeitaiClient.serves(platform):
            chain = ["keitai"] + chain
        for name in chain:
            try:
                res = self._clients[name].match(game)
                if res:
                    return res
            except Exception as exc:
                log.warning("fallback %s failed for %r: %s", name, title, exc)
        return None
