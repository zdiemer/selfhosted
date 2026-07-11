"""IGDB client + title matcher.

Auth is Twitch OAuth (client-credentials → bearer token, ~60d, auto-refreshed).
We fetch everything for a title in a SINGLE nested-field request (IGDB v4 lets
you expand related objects inline), so matching one spreadsheet row costs ~1
request instead of the dozen the original GamesMaster client made. Matching
reuses that project's battle-tested MatchValidator + platform-alias map.

Rate limited to IGDB's 4 req/s. Callers (the lazy enricher) serialize through
here, so a simple monotonic-spacing limiter is enough.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone

import requests

from excel_game import ExcelGame, ExcelPlatform
from match_validator import MatchValidator

log = logging.getLogger("gamedex.igdb")

_TWITCH_AUTH = "https://id.twitch.tv/oauth2/token"
_IGDB = "https://api.igdb.com/v4"

# One request pulls all candidates with everything we display, nested inline.
_FIELDS = (
    "fields name,slug,url,category,summary,storyline,"
    "first_release_date,total_rating,total_rating_count,rating,rating_count,aggregated_rating,"
    "alternative_names.name,platforms.name,release_dates.y,"
    "genres.name,themes.name,game_modes.name,player_perspectives.name,"
    "cover.image_id,screenshots.image_id,artworks.image_id,"
    "videos.video_id,videos.name,"
    "involved_companies.company.name,involved_companies.developer,"
    "involved_companies.publisher,"
    "franchises.name,franchise.name,"
    "similar_games.name,similar_games.slug,similar_games.url,"
    "similar_games.cover.image_id;"
)


def platform_from_str(value):
    if not value:
        return None
    try:
        return ExcelPlatform(value)
    except ValueError:
        return None


class RateLimiter:
    """Allow at most `rate` calls per second (monotonic spacing)."""

    def __init__(self, rate: int = 4):
        self._min_gap = 1.0 / rate
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self._min_gap


class IgdbClient:
    def __init__(self, client_id: str, client_secret: str, user_agent="gamedex"):
        self._client_id = client_id
        self._client_secret = client_secret
        self._ua = user_agent
        self._token = None
        self._token_expiry = 0.0
        self._auth_lock = threading.Lock()
        self._limiter = RateLimiter(4)
        self._validator = MatchValidator()

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # -- auth ---------------------------------------------------------------
    def _ensure_token(self):
        if self._token and time.time() < self._token_expiry - 60:
            return
        with self._auth_lock:
            if self._token and time.time() < self._token_expiry - 60:
                return
            resp = requests.post(
                _TWITCH_AUTH,
                params={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expiry = time.time() + int(data.get("expires_in", 3600))
            log.info("IGDB: obtained Twitch app token")

    def _post(self, route: str, body: str):
        self._ensure_token()
        self._limiter.wait()
        resp = requests.post(
            f"{_IGDB}/{route}",
            headers={
                "User-Agent": self._ua,
                "Client-ID": self._client_id,
                "Authorization": f"Bearer {self._token}",
            },
            data=body.encode("utf-8"),
            timeout=30,
        )
        if resp.status_code == 401:  # token rotated out early — refresh once
            self._token = None
            self._ensure_token()
            self._limiter.wait()
            resp = requests.post(
                f"{_IGDB}/{route}",
                headers={
                    "User-Agent": self._ua,
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {self._token}",
                },
                data=body.encode("utf-8"),
                timeout=30,
            )
        resp.raise_for_status()
        return resp.json()

    # -- search + match -----------------------------------------------------
    def _search(self, title: str):
        processed = unicodedata.normalize(
            "NFKD",
            title.replace("ū", "uu").replace("ō", "ou").replace("Ō", "Ou").replace("Ū", "Uu"),
        ).replace('"', '\\"')
        body = f'search "{processed}"; {_FIELDS} limit 25;'
        return self._post("games", body)

    def match(self, title, platform=None, release_year=None,
              developer=None, publisher=None, franchise=None):
        """Return (enrichment_dict, score) for the best acceptable candidate, or
        (None, 0) when nothing clears the confidence bar (blank-on-low-confidence)."""
        game = ExcelGame(
            title=title,
            platform=platform_from_str(platform),
            release_year=release_year,
            developer=developer,
            publisher=publisher,
            franchise=franchise,
        )
        candidates = self._search(title) or []
        best = None
        best_info = None
        for c in candidates:
            names = [c.get("name")] + [a["name"] for a in c.get("alternative_names", []) if a.get("name")]
            plat_names = [p["name"] for p in c.get("platforms", []) if p.get("name")]
            years = [rd["y"] for rd in c.get("release_dates", []) if rd.get("y")]
            if not years and c.get("first_release_date"):
                years = [datetime.fromtimestamp(c["first_release_date"], tz=timezone.utc).year]
            devs, pubs = [], []
            for ic in c.get("involved_companies", []):
                nm = (ic.get("company") or {}).get("name")
                if not nm:
                    continue
                if ic.get("developer"):
                    devs.append(nm)
                if ic.get("publisher"):
                    pubs.append(nm)
            frans = [f["name"] for f in c.get("franchises", []) if f.get("name")]
            if c.get("franchise", {}).get("name"):
                frans.append(c["franchise"]["name"])

            info = self._validator.validate(game, names, plat_names, years, pubs, devs, frans)
            # Accept only a confident match: title+platform agree, or an exact
            # title match when the candidate lists no platforms.
            accept = info.likely_match or (info.matched and not any(plat_names))
            if not accept:
                continue
            if best is None or info.match_score > best_info.match_score:
                best, best_info = c, info

        if best is None:
            return None, 0
        return self._to_enrichment(best, best_info), best_info.match_score

    def fetch_by_slug(self, slug: str):
        """Fetch a single game by its IGDB URL slug (for manual overrides)."""
        safe = slug.replace('"', "")
        res = self._post("games", f'{_FIELDS} where slug = "{safe}"; limit 1;')
        return res[0] if res else None

    def override_from_url(self, title, url):
        """Manual mapping: build an enrichment record from a pasted IGDB URL."""
        m = re.search(r"/games/([^/?#]+)", url)
        if not m:
            return None
        result = self.fetch_by_slug(m.group(1))
        return self.enrichment_from_result(result) if result else None

    def _to_enrichment(self, c, info):
        e = self.enrichment_from_result(c)
        e["confidence"] = info.match_score
        return e

    def enrichment_from_result(self, c):
        devs, pubs = [], []
        for ic in c.get("involved_companies", []):
            nm = (ic.get("company") or {}).get("name")
            if not nm:
                continue
            if ic.get("developer"):
                devs.append(nm)
            if ic.get("publisher"):
                pubs.append(nm)
        year = None
        if c.get("first_release_date"):
            year = datetime.fromtimestamp(c["first_release_date"], tz=timezone.utc).year
        rating = c.get("total_rating")
        user_rating = c.get("rating")            # IGDB community/user rating
        return {
            "igdbId": c.get("id"),
            "name": c.get("name"),
            "url": c.get("url"),
            "cover": (c.get("cover") or {}).get("image_id"),
            "summary": c.get("summary"),
            "storyline": c.get("storyline"),
            "rating": round(rating / 100, 4) if rating is not None else None,
            "ratingCount": c.get("total_rating_count"),
            "userRating": round(user_rating / 100, 4) if user_rating is not None else None,
            "userRatingCount": c.get("rating_count"),
            "year": year,
            "genres": [g["name"] for g in c.get("genres", []) if g.get("name")],
            "themes": [t["name"] for t in c.get("themes", []) if t.get("name")],
            "gameModes": [m["name"] for m in c.get("game_modes", []) if m.get("name")],
            "perspectives": [p["name"] for p in c.get("player_perspectives", []) if p.get("name")],
            "developers": sorted(set(devs)),
            "publishers": sorted(set(pubs)),
            "screenshots": [s["image_id"] for s in c.get("screenshots", []) if s.get("image_id")][:12],
            "artworks": [a["image_id"] for a in c.get("artworks", []) if a.get("image_id")][:6],
            "videos": [{"id": v["video_id"], "name": v.get("name")} for v in c.get("videos", []) if v.get("video_id")][:4],
            "similar": [
                {"name": s.get("name"), "url": s.get("url"),
                 "cover": (s.get("cover") or {}).get("image_id")}
                for s in c.get("similar_games", []) if s.get("name")
            ][:12],
            "confidence": None,
        }
