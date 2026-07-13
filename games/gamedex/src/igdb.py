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
    "similar_games.cover.image_id,"
    # Storefront ids — this is what lets the UI hand off to `steam://`.
    "external_games.external_game_source,external_games.uid,external_games.url;"
)

# IGDB's external_game_source enum, read off live data rather than guessed —
# the docs are thin and three of our first guesses were wrong (15 is Google Play,
# not itch; 36 is PlayStation, not Epic). The field used to be called `category`;
# it was renamed, and querying the old name silently returns nothing.
_STORE_SOURCE = {
    1: "steam",         # store.steampowered.com/app/<appid>
    5: "gog",           # gog.com — uid is the numeric product id
    11: "xbox",         # xbox.com/games/store — uid is the MS product id
    13: "appstore",     # itunes.apple.com/app/id<appid>
    15: "googleplay",   # play.google.com — uid is the package name
    23: "amazon",       # play.amazon.com — Amazon Games / Luna
    26: "epic",         # store.epicgames.com
    30: "itch",         # <dev>.itch.io/<game>
    36: "playstation",  # store.playstation.com/concept/<id>
    54: "microsoft",    # microsoft.com/p/... — the other MS store id
}


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

    # IGDB's game_type enum — what KIND of thing this entry is.
    GAME_TYPES = {
        0: "Main game", 1: "DLC", 2: "Expansion", 3: "Bundle",
        4: "Standalone expansion", 5: "Mod", 6: "Episode", 7: "Season",
        8: "Remake", 9: "Remaster", 10: "Expanded edition", 11: "Port",
        12: "Fork", 13: "Pack", 14: "Update",
    }

    _REL_FIELDS = (
        "fields id,game_type,version_title,"
        "parent_game.id,parent_game.name,parent_game.cover.image_id,"
        "version_parent.id,version_parent.name,version_parent.cover.image_id,"
        "dlcs.id,dlcs.name,dlcs.cover.image_id,"
        "expansions.id,expansions.name,expansions.cover.image_id,"
        "standalone_expansions.id,standalone_expansions.name,standalone_expansions.cover.image_id,"
        "expanded_games.id,expanded_games.name,expanded_games.cover.image_id,"
        "remakes.id,remakes.name,remakes.cover.image_id,"
        "remasters.id,remasters.name,remasters.cover.image_id,"
        "ports.id,ports.name,ports.cover.image_id,"
        "forks.id,forks.name,forks.cover.image_id,"
        "bundles.id,bundles.name,bundles.cover.image_id,"
        "collections.id,collections.name;"
    )

    _EPISODE, _SEASON, _BUNDLE = 6, 7, 3

    def relations_for(self, igdb_ids):
        """{igdb_id: relations} — the graph IGDB keeps and a spreadsheet can't.

        Fetched by id in batches of 500, like stores_for: asking for all of this
        on every *search* would bloat 25 results per query, and we only need it
        once per game.

        Two of the relationships have NO forward field and only exist as reverse
        links, so they need their own passes:
          episodes/seasons — an episode points UP via parent_game; the parent lists
            nothing. (Tales of Monkey Island is a plain Main game; its five chapters
            each have parent_game = it.)
          bundle contents  — a game in a bundle points UP via bundles; the bundle
            lists nothing.
        """
        out = {}
        for i in range(0, len(igdb_ids), 500):
            chunk = [int(x) for x in igdb_ids[i:i + 500]]
            body = f"{self._REL_FIELDS} where id = ({','.join(str(c) for c in chunk)}); limit 500;"
            for g in self._post("games", body) or []:
                rel = self._relations(g)
                if rel:
                    out[g["id"]] = rel

        ids = [int(x) for x in igdb_ids]
        self._add_episodes(out, ids)
        self._add_bundle_contents(out, ids)
        return out

    def _add_episodes(self, out, ids):
        """Attach episodes/seasons — the children that point up via parent_game."""
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            offset = 0
            while True:
                body = (
                    "fields id,name,game_type,parent_game,cover.image_id; "
                    f"where parent_game = ({','.join(str(c) for c in chunk)}) "
                    f"& game_type = ({self._EPISODE},{self._SEASON}); "
                    f"limit 500; offset {offset}; sort id asc;"
                )
                got = self._post("games", body) or []
                for g in got:
                    pid = g.get("parent_game")
                    if pid is None:
                        continue
                    rel = out.setdefault(pid, {"gameType": None, "gameTypeLabel": None})
                    key = "episodes" if g.get("game_type") == self._EPISODE else "seasons"
                    rel.setdefault(key, []).append(self._one(g))
                if len(got) < 500:
                    break
                offset += 500

    def _add_bundle_contents(self, out, ids):
        """Attach a bundle's contents — the games that point up via `bundles`."""
        bundles = {gid for gid, rel in out.items()
                   if rel.get("gameType") == self._BUNDLE and gid in set(ids)}
        bl = list(bundles)
        for i in range(0, len(bl), 400):
            chunk = bl[i:i + 400]
            offset = 0
            while True:
                body = (
                    "fields id,name,game_type,bundles,cover.image_id; "
                    f"where bundles = ({','.join(str(c) for c in chunk)}); "
                    f"limit 500; offset {offset}; sort id asc;"
                )
                got = self._post("games", body) or []
                for g in got:
                    for b in (g.get("bundles") or []):
                        if b in bundles:
                            out[b].setdefault("bundleContents", []).append(self._one(g))
                if len(got) < 500:
                    break
                offset += 500

    @staticmethod
    def _one(x):
        if not x:
            return None
        return {"id": x.get("id"), "name": x.get("name"),
                "cover": (x.get("cover") or {}).get("image_id")}

    @classmethod
    def _relations(cls, g):
        one = cls._one

        def many(key):
            return [one(x) for x in (g.get(key) or []) if x.get("name")]

        rel = {
            "gameType": g.get("game_type"),
            "gameTypeLabel": cls.GAME_TYPES.get(g.get("game_type")),
            "versionTitle": g.get("version_title"),
            "parent": one(g.get("parent_game")),
            "versionParent": one(g.get("version_parent")),
            "dlcs": many("dlcs"),
            "expansions": many("expansions"),
            "standaloneExpansions": many("standalone_expansions"),
            "expandedGames": many("expanded_games"),
            "remakes": many("remakes"),
            "remasters": many("remasters"),
            "ports": many("ports"),
            "forks": many("forks"),
            "bundles": many("bundles"),
            "collections": [c.get("name") for c in (g.get("collections") or []) if c.get("name")],
        }
        # Nothing but a game_type isn't a relationship worth storing.
        has_any = any(rel[k] for k in rel if k not in ("gameType", "gameTypeLabel"))
        return rel if has_any or rel["gameType"] else None

    def stores_for(self, igdb_ids):
        """{igdb_id: {'steam': '620', …}} for a batch of ids.

        Used to backfill storefront ids onto records matched before we started
        asking for them. Fetching by id lets us do 500 games per request instead
        of one search each — the whole 14.5k library is ~30 calls.
        """
        out = {}
        for i in range(0, len(igdb_ids), 500):
            chunk = [int(x) for x in igdb_ids[i:i + 500]]
            # .url matters: for Epic and itch there's no way to build a link from
            # the id alone, so a record backfilled without it has no button at all.
            body = ("fields id,external_games.external_game_source,external_games.uid,"
                    "external_games.url; "
                    f"where id = ({','.join(str(c) for c in chunk)}); limit 500;")
            for g in self._post("games", body) or []:
                st = self._stores(g.get("external_games") or [])
                if st:
                    out[g["id"]] = st
        return out

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

    @staticmethod
    def _franchises_of(c):
        """A game's franchises, as names. IGDB splits these across `franchise` (the
        primary one) and `franchises` (all of them), and either can be absent — merge
        both and de-dup while preserving order (primary first)."""
        out = []
        main = (c.get("franchise") or {}).get("name")
        if main:
            out.append(main)
        for f in c.get("franchises") or []:
            nm = f.get("name")
            if nm and nm not in out:
                out.append(nm)
        return out

    def franchises_for(self, igdb_ids):
        """{igdb_id: [franchise names]} — fetched by id in batches, to backfill records
        matched before franchises were stored. Mirrors relations_for."""
        out = {}
        for i in range(0, len(igdb_ids), 500):
            chunk = [int(x) for x in igdb_ids[i:i + 500]]
            body = ("fields id,franchise.name,franchises.name; "
                    f"where id = ({','.join(str(c) for c in chunk)}); limit 500;")
            for g in self._post("games", body) or []:
                out[g["id"]] = self._franchises_of(g)
        return out

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
            "franchises": self._franchises_of(c),
            "screenshots": [s["image_id"] for s in c.get("screenshots", []) if s.get("image_id")][:12],
            "artworks": [a["image_id"] for a in c.get("artworks", []) if a.get("image_id")][:6],
            "videos": [{"id": v["video_id"], "name": v.get("name")} for v in c.get("videos", []) if v.get("video_id")][:4],
            "similar": [
                {"name": s.get("name"), "url": s.get("url"),
                 "cover": (s.get("cover") or {}).get("image_id")}
                for s in c.get("similar_games", []) if s.get("name")
            ][:12],
            "stores": self._stores(c.get("external_games") or []),
            "confidence": None,
        }

    @staticmethod
    def _stores(external):
        """{'steam': {'id': '620', 'url': '…'}, …}

        The id is what a launch URI needs; the url is what we fall back to when a
        storefront has no launch scheme (most of them).
        """
        out = {}
        for e in external:
            name = _STORE_SOURCE.get(e.get("external_game_source"))
            uid = e.get("uid")
            if not name or not uid or name in out:
                continue
            out[name] = {"id": str(uid), "url": e.get("url") or None}
        return out
