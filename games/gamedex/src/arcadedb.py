"""Arcade Database (adb.arcadeitalia.net) — arcade cabinet metadata and artwork.

Unique among our sources: it's keyed on the MAME romset, which the sheet already
records, so this is an EXACT lookup rather than a fuzzy title match. It cannot
mismatch, and it needs no MatchValidator.

Gives us the things IGDB has least of for arcade games: cabinet, marquee, flyer
and title-screen scans, plus player count, control layout, screen orientation,
manufacturer and the MAME history blurb.
"""

from __future__ import annotations

import logging
import re

import requests

from igdb import RateLimiter

log = logging.getLogger("gamedex.arcadedb")

_URL = "https://adb.arcadeitalia.net/service_scraper.php"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _clean(v):
    """ADB mixes types: `players` comes back as an int, most fields as strings."""
    if v is None:
        return None
    v = str(v).strip()
    return v or None


class ArcadeDbClient:
    """Secondary source, gated to games that carry a MAME romset."""

    def __init__(self):
        self._limiter = RateLimiter(4)      # the client in GamesMaster uses 4/s

    @property
    def configured(self):
        return True

    def _query(self, romset):
        self._limiter.wait()
        r = requests.get(
            _URL,
            params={"ajax": "query_mame", "game_name": romset, "use_parent": 1,
                    "resize": 0, "lang": "en"},
            headers={"User-Agent": _UA}, timeout=30,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("result") or []
        # An unknown romset comes back as one entry with an empty title.
        return next((x for x in results if _clean(x.get("title"))), None)

    def match_meta(self, meta):
        """Enricher entry point — needs the romset, not just the title."""
        romset = meta.get("mameRomset")
        if not romset:
            return None
        return self._to_record(self._query(romset.strip()))

    def match(self, title, platform=None, year=None):
        return None                          # romset-only; see match_meta

    def override_from_url(self, title, url):
        """Manual mapping from an ADB url (…/?mame=sf2)."""
        m = re.search(r"[?&]mame=([A-Za-z0-9_]+)", url or "")
        if not m:
            return None
        return self._to_record(self._query(m.group(1)))

    def _to_record(self, r):
        if not r:
            return None
        # nplayers reads like "2P alt" / "4P sim"; players is the raw count.
        players = _clean(r.get("players"))
        return {
            "name": _clean(r.get("title")),
            "romset": _clean(r.get("game_name")),
            "url": _clean(r.get("url")),
            "manufacturer": _clean(r.get("manufacturer")),
            "year": _clean(r.get("year")),
            "genre": _clean(r.get("genre")),
            "players": int(players) if players and players.isdigit() else None,
            "playersDetail": _clean(r.get("nplayers")),
            "controls": _clean(r.get("input_controls")),
            "buttons": _clean(r.get("input_buttons")),
            "orientation": _clean(r.get("screen_orientation")),
            "resolution": _clean(r.get("screen_resolution")),
            "series": _clean(r.get("serie")),
            "history": _clean(r.get("history")),
            "rating": _clean(r.get("rate")),
            "cabinet": _clean(r.get("url_image_cabinet")),
            "marquee": _clean(r.get("url_image_marquee")),
            "flyer": _clean(r.get("url_image_flyer")),
            "titleScreen": _clean(r.get("url_image_title")),
            "ingame": _clean(r.get("url_image_ingame")),
            "video": _clean(r.get("url_video_shortplay_hd")) or _clean(r.get("url_video_shortplay")),
        }
