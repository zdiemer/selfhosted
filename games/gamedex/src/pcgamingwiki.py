"""PCGamingWiki — will it actually run properly on my machine?

The one question nothing else in the app answers. IGDB tells you a game exists on PC; Steam
tells you whether it runs on the Deck. Neither tells you whether it supports ultrawide, or
whether it's a 32-bit D3D9 executable from 2007 that will letterbox itself on your monitor.
PCGamingWiki is the wiki that documents exactly this, and it exposes its infoboxes as
structured data through MediaWiki's Cargo extension — free, unauthenticated, and queryable.

Two things make this the cheapest source in the app:

  * The join is EXACT. PCGamingWiki records the Steam AppID, and so do we (IGDB's
    `stores.steam.id`). There is no title matching here and therefore no wrong match —
    either the appid is on the page or it isn't. Contrast every other secondary source,
    which is a fuzzy title match with a confidence score and a chance of being wrong.
  * There are no per-game requests at all. Cargo will return 500 joined rows per call with
    no `where` clause, so the whole dataset comes down in ~100 requests and is cached on
    the PVC — the same trick gametdb.py plays with its dumps. A lookup is then a dict hit.

The `Special:` HTML pages sit behind Cloudflare and will 403 a script; `api.php` does not.
Don't "fix" a 403 by adding browser impersonation — query the API instead.

Field names are NOT guessable and several plausible ones don't exist (`Field_of_view_FOV`,
`Anti_aliasing_AA`, `Availability.DRM` are all invalid). Every name below was verified
against the live API before it was written down.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import threading
import time

import requests

log = logging.getLogger("gamedex.pcgamingwiki")

API = "https://www.pcgamingwiki.com/w/api.php"
PAGE = "https://www.pcgamingwiki.com/wiki/{}"
_UA = "gamedex/1.0 (personal game collection; +https://github.com/zdiemer)"

# Only PC-family platforms have a Steam appid to join on.
PLATFORMS = {"PC", "Mac", "Linux"}

# The five Cargo tables worth joining, and the fields on each that survived verification.
# Joined on _pageID (not _pageName) — a page's rows across tables share the page id.
_TABLES = "Infobox_game,Video,API,Input,Audio"
_JOIN = ("Infobox_game._pageID=Video._pageID,"
         "Infobox_game._pageID=API._pageID,"
         "Infobox_game._pageID=Input._pageID,"
         "Infobox_game._pageID=Audio._pageID")
_FIELDS = ",".join([
    "Infobox_game._pageName=Page",
    "Infobox_game.Steam_AppID=AppID",
    "Video.Widescreen_resolution=Widescreen",
    "Video.Ultrawidescreen=Ultrawide",
    "Video.4K_Ultra_HD=UHD",
    "Video.HDR=HDR",
    "Video.Ray_tracing=RayTracing",
    "API.Direct3D_versions=D3D",
    "API.OpenGL_versions=OpenGL",
    "API.Vulkan_versions=Vulkan",
    "API.Windows_64bit_executable=Win64",
    "Input.Full_controller_support=Controller",
    "Audio.Surround_sound=Surround",
])
_PAGE_SIZE = 500          # Cargo's own cap
_MAX_PAGES = 400          # a backstop, not a target: 400 x 500 = 200k rows

# PCGamingWiki rate-limits, and it does not warn you: it served 34 pages happily and then
# 429'd. So pace the walk, and back off and retry rather than abandoning the pass — a dump
# that stops a third of the way through is a dump that silently has no answer for two
# thirds of the library.
# Their limit is on request RATE, not query cost: a cheap keyset page 429s just the same
# once you've made ~40 of them in a minute. So the fix is to be slower, not cleverer.
_PAGE_DELAY = 3.0         # between pages, unconditionally — ~20 req/min, under their limit
_RETRIES = 6
_BACKOFF = 15.0           # seconds, doubling: 15, 30, 60, 120, 240, 480

# Cargo answers with the wiki's own vocabulary. "unknown" and "" both mean "nobody has
# filled this in", which is not the same as false and must not be shown as false.
_UNSET = {"", "unknown", "n/a", "na"}


def _val(v):
    v = (v or "").strip()
    return None if v.lower() in _UNSET else v


class PcGamingWiki:
    def __init__(self, cache_dir: str = "/data/pcgamingwiki", ttl_days: int = 14):
        self._dir = pathlib.Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "pcgw.json"
        self._ttl = ttl_days * 86400
        self._map: dict[str, dict] = {}       # steam appid -> record
        self._after = ""                      # cursor: the last page name a pass reached
        self._complete = False
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._load()

    @property
    def ready(self) -> bool:
        """Only once the WHOLE table is in.

        A partial map is worse than no map: the enricher would resolve every game that
        happens to be missing from it to a `no_match`, and no_match is written down once and
        never revisited. Two thirds of the PC library would be permanently marked as
        "PCGamingWiki has nothing on this" when the truth is we simply hadn't fetched it yet.
        """
        return self._complete and bool(self._map)

    def serves(self, platform: str | None) -> bool:
        return platform in PLATFORMS

    # -- the dump ------------------------------------------------------------
    def _load(self) -> None:
        try:
            blob = json.loads(self._path.read_text())
            if blob.get("version") == 1:
                # A partial dump is still worth loading: it means the next pass RESUMES from
                # where the rate limiter cut us off instead of re-walking pages we already
                # have (and getting 429'd again on the way back to where we were).
                self._map = blob.get("games") or {}
                self._after = blob.get("after") or ""
                self._complete = bool(blob.get("complete"))
                fresh = time.time() - blob.get("fetched", 0) < self._ttl
                if self._complete and fresh:
                    log.info("pcgamingwiki: %d Steam appids from cache", len(self._map))
                    return
                if self._map:
                    log.info("pcgamingwiki: resuming after %r (%d appids so far)",
                             self._after[:30], len(self._map))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("pcgamingwiki: cache unreadable (%s)", exc)
        # A cold walk is ~100 sequential calls and the wiki rate-limits, so this is minutes —
        # far too long to hold up app startup (the rollout would time out before the pod ever
        # went Ready). Fetch in the background; `ready` stays False until the WHOLE table is
        # in, and the enricher requeues rather than recording a no_match it'd never revisit.
        threading.Thread(target=self._refresh_until_done, name="pcgw-dump", daemon=True).start()

    def _refresh_until_done(self) -> None:
        """Keep going until the table is complete. Rate limiting delays us; it doesn't stop us."""
        attempt = 0
        while not self._complete:
            self.refresh()
            if self._complete:
                return
            attempt += 1
            wait = min(60 * 2 ** min(attempt, 4), 900)     # 2m, 4m, 8m, 15m, 15m…
            log.info("pcgamingwiki: partial (%d appids, after %r) — retrying in %.0fs",
                     len(self._map), self._after[:30], wait)
            time.sleep(wait)

    def _query(self, after: str) -> list[dict]:
        """One page, walked by KEY rather than by offset, with backoff.

        `offset=15000` makes the wiki scan and discard 15,000 joined rows before it can
        answer, so each page costs more than the last and it starts 429ing — which is
        exactly what it did. Ordering by page name and asking for "the 500 after this one"
        costs the same every time, however deep into the walk we are.
        """
        params = {
            "action": "cargoquery", "format": "json",
            "tables": _TABLES, "join_on": _JOIN, "fields": _FIELDS,
            "order_by": "Infobox_game._pageName ASC", "limit": _PAGE_SIZE,
        }
        if after:
            # SQL string literal: a page name may well contain an apostrophe
            # ("Assassin's Creed"), and doubling it is how SQL escapes one.
            params["where"] = f"Infobox_game._pageName > '{after.replace(chr(39), chr(39) * 2)}'"
        wait = _BACKOFF
        for attempt in range(_RETRIES):
            r = self._s.get(API, timeout=60, params=params)
            if r.status_code == 429:
                # Retry-After is a FLOOR to respect, never a licence to retry immediately.
                # This wiki serves `Retry-After: 0` while it is still refusing you, and
                # trusting that number burned all five attempts inside a second — the retry
                # budget evaporated without a single one of them ever having waited.
                retry_after = r.headers.get("Retry-After") or ""
                hinted = float(retry_after) if retry_after.isdigit() else 0.0
                delay = max(hinted, wait)
                log.info("pcgamingwiki: 429 after %r — waiting %.0fs (attempt %d/%d)",
                         after[:30], delay, attempt + 1, _RETRIES)
                time.sleep(delay)
                wait *= 2
                continue
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"].get("info", "cargo error"))
            return [row["title"] for row in j.get("cargoquery", [])]
        raise RuntimeError(f"rate-limited after {after!r} ({_RETRIES} attempts)")

    def refresh(self) -> None:
        """Pull the joined table down, 500 rows at a time, and index it by appid.

        Resumes from wherever the last pass stopped, so being rate-limited costs us the
        remainder of the walk and not the walk itself.
        """
        games: dict[str, dict] = dict(self._map)
        after, pages = self._after, 0
        complete = False
        try:
            while pages < _MAX_PAGES:
                if pages:
                    time.sleep(_PAGE_DELAY)     # be a guest, not a scraper
                rows = self._query(after)
                if not rows:
                    complete = True
                    break
                for t in rows:
                    # Steam_AppID is a Cargo LIST field: one page can carry several appids
                    # (Portal is "400,323170" — the game and its soundtrack/companion entry).
                    # Index under each, so a lookup by any of them finds the page.
                    ids = [i for i in re.split(r"[,\s]+", (t.get("AppID") or "")) if i.isdigit()]
                    if ids:
                        rec = self._record(t)
                        for appid in ids:
                            games.setdefault(appid, rec)
                # The cursor is the last page NAME we saw — including for rows with no appid,
                # or the walk would stall on a run of them and ask for the same page forever.
                last = (rows[-1].get("Page") or "").strip()
                if not last or last == after:
                    complete = True         # can't advance: stop rather than loop
                    break
                after = last
                pages += 1
                if len(rows) < _PAGE_SIZE:
                    complete = True
                    break
        except Exception as exc:
            log.warning("pcgamingwiki: refresh stopped after %r (%s)", after[:40], exc)
        if not games:
            return                          # keep whatever we had; a stale map beats none

        self._map = games
        self._after = after
        self._complete = complete
        # Persist the partial too — with `complete: false`, so a truncated pass can never be
        # mistaken for the whole table (which would make `ready` true and let the enricher
        # write a permanent no_match for every game we simply hadn't reached yet).
        try:
            self._path.write_text(json.dumps({
                "fetched": time.time(), "version": 1, "games": games,
                "after": after, "complete": complete,
            }))
        except Exception as exc:
            log.warning("pcgamingwiki: could not write cache (%s)", exc)
        log.info("pcgamingwiki: %d Steam appids indexed, %d pages (%s)", len(games), pages,
                 "COMPLETE" if complete else "partial — will resume")

    @staticmethod
    def _record(t: dict) -> dict:
        page = t.get("Page") or ""
        return {
            "source": "PCGamingWiki",
            "name": page,
            "url": PAGE.format(page.replace(" ", "_")),
            "widescreen": _val(t.get("Widescreen")),
            "ultrawide": _val(t.get("Ultrawide")),
            "uhd4k": _val(t.get("UHD")),
            "hdr": _val(t.get("HDR")),
            "rayTracing": _val(t.get("RayTracing")),
            "d3d": _val(t.get("D3D")),
            "opengl": _val(t.get("OpenGL")),
            "vulkan": _val(t.get("Vulkan")),
            "win64": _val(t.get("Win64")),
            "controller": _val(t.get("Controller")),
            "surround": _val(t.get("Surround")),
            # An appid join cannot be wrong, so this is not a guess like the other sources'
            # title matches are. Full marks, and the Health tab can tell the two apart.
            "confidence": 15,
        }

    # -- lookup --------------------------------------------------------------
    def match_meta(self, meta: dict):
        """Exact lookup on the Steam appid the enricher hands us. No network, no matching."""
        if not self.serves(meta.get("platform")):
            return None
        appid = str(meta.get("steamAppId") or "").strip()
        if not appid or not self._map:
            return None
        return self._map.get(appid)

    def match(self, title: str, platform=None, year=None):
        return None                     # appid-keyed; the entry point is match_meta

    def override_from_url(self, title: str, url: str):
        """Paste a PCGamingWiki page URL and it's pinned. Fetches that one page's row."""
        m = re.search(r"pcgamingwiki\.com/wiki/([^?#]+)", (url or "").strip())
        if not m:
            return None
        page = m.group(1).replace("_", " ")
        try:
            r = self._s.get(API, timeout=30, params={
                "action": "cargoquery", "format": "json",
                "tables": _TABLES, "join_on": _JOIN, "fields": _FIELDS,
                "limit": 1, "where": f'Infobox_game._pageName="{page}"',
            })
            r.raise_for_status()
            rows = [row["title"] for row in (r.json().get("cargoquery") or [])]
        except Exception:
            return None
        return self._record(rows[0]) if rows else None
