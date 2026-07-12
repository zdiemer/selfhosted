"""RomM — a Play button for the games we can actually emulate in the browser.

The join is exact on both axes, which is why this is worth doing at all:

  * gamedex already knows each row's IGDB *game* id (from enrichment)
  * RomM stores an `igdb_id` on every ROM it has matched

An id join, not a title match. But a game id alone is ambiguous — Doom exists on
3DO, PSX and DOS — so the key is (igdb_id, platform). RomM reports each ROM's
`platform_fs_slug`, which is literally the folder name on the NAS ("3DO", "PSX"),
and the sheet has its own name for the same machine ("PlayStation"). PLATFORMS
below reconciles the two; anything not in it must match by name.

Only the mapping reaches the browser — never the credentials. The frontend gets
{"<igdb_id>|<folder>": rom_id} and builds <publicUrl>/console/rom/<id>/play.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
import urllib.request
import json

log = logging.getLogger("gamedex.romm")

PAGE = 500          # roms per request
REFRESH = 900       # re-pull every 15 min: a scan adds games while we run


class RommClient:
    def __init__(self, base_url: str, public_url: str, username: str, password: str):
        self.base = (base_url or "").rstrip("/")
        self.public = (public_url or "").rstrip("/")
        self.user = username
        self.pw = password
        self._token = None
        self._token_exp = 0.0
        self._map: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stamp = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.user and self.pw)

    # -- auth ---------------------------------------------------------------
    def _access_token(self) -> str | None:
        """RomM's /api/token is an oauth2 password grant, and CSRF-exempt."""
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "password",
            "username": self.user,
            "password": self.pw,
            "scope": "roms.read platforms.read",
        }).encode()
        req = urllib.request.Request(f"{self.base}/api/token", data=body)
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
        self._token = tok["access_token"]
        # `expires` is seconds; be conservative if RomM ever stops sending it.
        self._token_exp = time.time() + float(tok.get("expires") or 900)
        return self._token

    def _get(self, path: str):
        req = urllib.request.Request(
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    # -- the mapping --------------------------------------------------------
    def refresh(self) -> int:
        """(igdb_id, platform folder) -> rom id, for every matched ROM."""
        out: dict[str, int] = {}
        offset = 0
        while True:
            page = self._get(f"/api/roms?limit={PAGE}&offset={offset}")
            items = page.get("items") or []
            for rom in items:
                igdb = rom.get("igdb_id")
                plat = rom.get("platform_fs_slug")
                if not igdb or not plat:
                    continue          # unmatched ROM: nothing to join on
                key = f"{igdb}|{plat}"
                # First one wins. A game can have several ROMs on one platform
                # (regions, revisions); any of them plays.
                out.setdefault(key, rom["id"])
            total = page.get("total") or 0
            offset += len(items)
            if not items or offset >= total:
                break
        with self._lock:
            self._map = out
            self._stamp = time.time()
        log.info("romm: %d playable games mapped", len(out))
        return len(out)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "baseUrl": self.public,
                "roms": dict(self._map),
                "updated": self._stamp,
            }

    def start(self):
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:      # RomM restarting, scan mid-flight, …
                    log.warning("romm refresh failed: %s", e)
                time.sleep(REFRESH)
        threading.Thread(target=loop, daemon=True, name="romm").start()
