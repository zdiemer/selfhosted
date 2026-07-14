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
{"<igdb_id>|<folder>": rom_id} and builds a Console Mode link on desktop, or
the <publicUrl>/rom/<id> page on mobile.
"""

from __future__ import annotations

import logging
import pathlib
import threading
import time
import urllib.parse
import urllib.request
import json

log = logging.getLogger("gamedex.romm")

PAGE = 500          # roms per request
REFRESH = 900       # re-pull every 15 min: a scan adds games while we run


class RommClient:
    def __init__(self, base_url: str, public_url: str, username: str, password: str,
                 cache_path: str = "/data/romm-map.json"):
        self.base = (base_url or "").rstrip("/")
        self.public = (public_url or "").rstrip("/")
        self.user = username
        self.pw = password
        self._token = None
        self._token_exp = 0.0
        self._map: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stamp = 0.0
        self._cache = pathlib.Path(cache_path)
        self._load_cache()

    def _load_cache(self) -> None:
        """Start from the last known map instead of from nothing.

        The map lived only in memory, and a full scan of the library takes a quarter of an
        hour — so EVERY restart left the whole library unplayable until it finished. Deploy
        the app and the Play buttons are simply gone for fifteen minutes, which is exactly
        what it looked like when RomM "broke". A rom id doesn't go stale in any way that
        matters (a dead id 404s on click, and the next scan corrects it), so serving a slightly
        old map beats serving none."""
        try:
            data = json.loads(self._cache.read_text())
            roms = data.get("roms") or {}
            if isinstance(roms, dict) and roms:
                self._map = {k: int(v) for k, v in roms.items()}
                self._stamp = float(data.get("updated") or 0)
                log.info("romm: %d playable games restored from cache", len(self._map))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("romm: cache unreadable (%s); starting empty", exc)

    def _save_cache(self) -> None:
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"roms": self._map, "updated": self._stamp}))
            tmp.replace(self._cache)                 # atomic: never a half-written map
        except Exception as exc:
            log.warning("romm: could not write cache (%s)", exc)

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
        """(igdb_id, platform folder) -> rom id, for every matched ROM.

        GROW, then SWAP. The library is tens of thousands of ROMs and takes many pages, and
        this runs every 15 minutes — so the published map must never be a PARTIAL one.

        It used to be. Each page did `self._map = dict(out)`, replacing the live map with
        "everything scanned so far", which at offset 0 means a few hundred entries. So every
        quarter of an hour the map collapsed from thousands to almost nothing and climbed
        back, and the Play buttons vanished from the whole library while it did. If you
        happened to load the page during a rescan, RomM looked broken.

        Now each page is MERGED into the live map (which therefore only ever grows), and the
        freshly-scanned set replaces it wholesale only once the scan has actually finished —
        which is also what lets a deleted ROM eventually disappear. A failed page leaves the
        old entries in place rather than dropping them.
        """
        out: dict[str, int] = {}
        offset = 0
        complete = False
        while True:
            try:
                page = self._get(f"/api/roms?limit={PAGE}&offset={offset}")
            except Exception as e:
                log.warning("romm: page at offset %d failed (%s); keeping %d so far",
                            offset, e, len(out))
                break
            items = page.get("items") or []
            for rom in items:
                igdb = rom.get("igdb_id")
                plat = rom.get("platform_fs_slug")
                if not igdb or not plat:
                    continue          # unmatched ROM: nothing to join on
                # First one wins. A game can have several ROMs on one platform
                # (regions, revisions); any of them plays.
                out.setdefault(f"{igdb}|{plat}", rom["id"])
            # Merge, never shrink: a slow or half-scanned RomM lights up what we have
            # WITHOUT taking away what we already had.
            with self._lock:
                merged = dict(self._map)
                merged.update(out)
                self._map = merged
                self._stamp = time.time()
            total = page.get("total") or 0
            offset += len(items)
            if not items or offset >= total:
                complete = True
                break

        if complete:
            # The scan finished, so this set is authoritative — swap it in, which also drops
            # roms that have gone away. A partial scan never gets to do this.
            with self._lock:
                self._map = out
                self._stamp = time.time()
            self._save_cache()          # survive the next restart
        log.info("romm: %d playable games mapped%s", len(out),
                 "" if complete else " (partial scan — kept previous entries)")
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
