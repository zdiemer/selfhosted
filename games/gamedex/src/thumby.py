"""Thumby / Thumby Color — TinyCircuits' own game lists.

There are only 8 of these in the sheet, and essentially nothing else on the web
carries metadata for them: no IGDB entry, no Metacritic, no HLTB. TinyCircuits
publishes a url_list.txt per device — NAME= blocks followed by the game's asset
URLs (source, description, title video) — so that list IS the catalogue.

Both lists are small, so we fetch each once and cache it for the process.
"""

from __future__ import annotations

import logging
import threading

import requests

from excel_game import ExcelGame
from igdb import RateLimiter, platform_from_str
from match_validator import MatchValidator

log = logging.getLogger("gamedex.thumby")

_RAW = "https://raw.githubusercontent.com/TinyCircuits"
_LISTS = {
    "Thumby": f"{_RAW}/TinyCircuits-Thumby-Games/master/url_list.txt",
    "Thumby Color": f"{_RAW}/TinyCircuits-Thumby-Color-Games/master/url_list.txt",
}
_PLATFORMS = set(_LISTS)


def _repo_url(urls):
    """An asset's raw URL → the game's folder on github.com."""
    if not urls:
        return None
    return (urls[0].rsplit("/", 1)[0]
            .replace("raw.githubusercontent.com", "github.com")
            .replace("/master/", "/blob/master/")
            .replace("/main/", "/tree/main/"))


class ThumbyClient:
    def __init__(self):
        self._validator = MatchValidator()
        self._limiter = RateLimiter(4)
        self._lock = threading.Lock()
        self._catalog = None            # [(platform, name, [urls])]

    @property
    def configured(self):
        return True

    def _load(self):
        with self._lock:
            if self._catalog is not None:
                return self._catalog
            catalog = []
            for platform, url in _LISTS.items():
                try:
                    self._limiter.wait()
                    r = requests.get(url, timeout=30)
                    r.raise_for_status()
                except Exception as exc:
                    log.warning("thumby: could not fetch %s list: %s", platform, exc)
                    continue
                for block in r.text.split("NAME=")[1:]:
                    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
                    if lines:
                        catalog.append((platform, lines[0], lines[1:]))
            self._catalog = catalog
            log.info("thumby: catalogue loaded (%d games)", len(catalog))
            return catalog

    def match_meta(self, meta):
        platform = meta.get("platform")
        if platform not in _PLATFORMS:
            return None
        return self.match(meta["title"], platform, meta.get("year"))

    def match(self, title, platform=None, year=None):
        if platform not in _PLATFORMS:
            return None
        game = ExcelGame(title=title, platform=platform_from_str(platform), release_year=year)
        for plat, name, urls in self._load():
            if plat != platform:
                continue
            if (self._validator.titles_equal_normalized(title, name)
                    or self._validator.titles_equal_fuzzy(title, name)):
                return self._to_record(plat, name, urls)
        return None

    def override_from_url(self, title, url):
        """Manual mapping: pin by the game's folder name on GitHub."""
        want = (url or "").rstrip("/").rsplit("/", 1)[-1].lower()
        for plat, name, urls in self._load():
            folder = (_repo_url(urls) or "").rstrip("/").rsplit("/", 1)[-1].lower()
            if want and (want == folder or want == name.lower()):
                return self._to_record(plat, name, urls)
        return None

    def _to_record(self, platform, name, urls):
        desc_url = next((u for u in urls if "_description" in u), None)
        description = None
        if desc_url:
            try:
                self._limiter.wait()
                r = requests.get(desc_url, timeout=30)
                if r.ok:
                    description = r.text.strip() or None
            except Exception as exc:
                log.debug("thumby: description fetch failed for %s: %s", name, exc)
        return {
            "name": name,
            "platform": platform,
            "url": _repo_url(urls),
            "description": description,
            "video": next((u for u in urls if u.endswith(".webm")), None),
            "source": next((u for u in urls if u.endswith(".py")), None),
        }
