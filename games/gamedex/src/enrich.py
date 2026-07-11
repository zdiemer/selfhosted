"""Lazy, host-cached IGDB enrichment.

Enrichment is on-demand: the frontend asks for the games currently on screen,
those matchKeys are queued, a single worker resolves them against IGDB at 4
req/s, and results are cached in a SQLite file on the mounted PVC. Browsed games
enrich in seconds and stay cached forever; games you never open cost nothing.

An optional (off-by-default) backfill slowly enriches the rest so a full IGDB
dataset can eventually be built without hammering the API.

matchKeys are computed here (normalized title | platform | year) and stamped
onto every served row as `_k`, so the frontend only sends keys — the server
already holds the title/platform/metadata needed to run a match.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

from match_validator import MatchValidator

log = logging.getLogger("gamedex.enrich")

# Light projection shipped for a whole page (covers + facets-worthy bits).
_LIGHT = ("igdbId", "cover", "rating", "year", "genres", "themes", "gameModes", "name")
# Even leaner projection for the bulk facet/cover map (all matched games).
_FACET_LIGHT = ("cover", "genres", "themes", "gameModes")

_SHEET_TITLE = {"games": "title", "completed": "game", "onOrder": "title"}


class Enricher:
    def __init__(self, client, db_path: str, backfill: bool = False):
        self._client = client
        self._db_path = db_path
        self._backfill = backfill
        self._validator = MatchValidator()

        self._key_meta: dict = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: deque = deque()
        self._queued: set = set()
        self._stop = threading.Event()
        self._worker = None

        self._db_lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS enrichment("
            " match_key TEXT PRIMARY KEY, igdb_id INTEGER, status TEXT,"
            " score INTEGER, data TEXT, updated_at TEXT)"
        )
        self._db.commit()

    # -- match keys ---------------------------------------------------------
    def key_for(self, title, platform, year) -> str:
        return f"{self._validator.normalize(title)}|{(platform or '').lower()}|{year or ''}"

    def reindex(self, parsed: dict):
        """Stamp `_k` on every row and (re)build the key→metadata map. Called
        after each dataset (re)load; mutates `parsed` in place before it's served."""
        key_meta = {}
        for sheet, title_field in _SHEET_TITLE.items():
            for r in parsed.get(sheet, {}).get("rows", []):
                title = r.get(title_field)
                if not title:
                    continue
                k = self.key_for(title, r.get("platform"), r.get("releaseYear"))
                r["_k"] = k
                if k not in key_meta:
                    key_meta[k] = {
                        "title": title,
                        "platform": r.get("platform"),
                        "year": r.get("releaseYear"),
                        "developer": r.get("developer"),
                        "publisher": r.get("publisher"),
                        "franchise": r.get("franchise"),
                    }
        with self._lock:
            self._key_meta = key_meta
        if self._backfill:
            self.enqueue_missing(list(key_meta.keys()))
        log.info("enrich: indexed %d unique match keys (backfill=%s)", len(key_meta), self._backfill)

    # -- db helpers ---------------------------------------------------------
    def _get_rows(self, keys):
        if not keys:
            return {}
        out = {}
        with self._db_lock:
            qs = ",".join("?" * len(keys))
            for mk, status, data in self._db.execute(
                f"SELECT match_key,status,data FROM enrichment WHERE match_key IN ({qs})", keys
            ):
                out[mk] = (status, json.loads(data) if data else None)
        return out

    def _save(self, key, enrichment, score):
        status = "matched" if enrichment else "no_match"
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO enrichment VALUES(?,?,?,?,?,?)",
                (
                    key,
                    enrichment.get("igdbId") if enrichment else None,
                    status,
                    score,
                    json.dumps(enrichment) if enrichment else None,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            self._db.commit()

    def _cached_status(self, key):
        with self._db_lock:
            row = self._db.execute(
                "SELECT status FROM enrichment WHERE match_key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    # -- request / read -----------------------------------------------------
    def request(self, keys, front=True) -> int:
        """Queue any of `keys` not already cached/queued. On-demand requests go
        to the front of the line so on-screen covers resolve first."""
        added = 0
        with self._cv:
            for k in keys:
                if k in self._queued or k not in self._key_meta:
                    continue
                if self._cached_status(k):
                    continue
                (self._queue.appendleft if front else self._queue.append)(k)
                self._queued.add(k)
                added += 1
            if added:
                self._cv.notify()
        return added

    def enqueue_missing(self, keys):
        self.request(keys, front=False)

    def get_light(self, keys):
        """Return {key: light-enrichment} for matched keys + the list still pending."""
        rows = self._get_rows(keys)
        items, pending = {}, []
        for k in keys:
            entry = rows.get(k)
            if entry is None:
                pending.append(k)
            elif entry[0] == "matched":
                items[k] = {f: entry[1].get(f) for f in _LIGHT}
        return items, pending

    def get_detail(self, key):
        """Return (status, detail): status is 'matched' | 'no_match' | 'pending'.
        A pending key is (re)queued at the front so the drawer resolves quickly;
        a no_match key is terminal so the UI stops polling."""
        rows = self._get_rows([key])
        entry = rows.get(key)
        if entry:
            return entry[0], (entry[1] if entry[0] == "matched" else None)
        self.request([key])
        return "pending", None

    def get_all_light(self):
        """{matchKey: {cover, genres, themes, gameModes}} for every matched game.
        Powers the global cover map + IGDB facets once the cache is populated."""
        out = {}
        with self._db_lock:
            for mk, data in self._db.execute(
                "SELECT match_key,data FROM enrichment WHERE status='matched'"
            ):
                if not data:
                    continue
                d = json.loads(data)
                out[mk] = {f: d.get(f) for f in _FACET_LIGHT}
        return out

    def stats(self):
        with self._db_lock:
            matched = self._db.execute(
                "SELECT COUNT(*) FROM enrichment WHERE status='matched'"
            ).fetchone()[0]
            total = self._db.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0]
        with self._lock:
            return {
                "total": len(self._key_meta),
                "resolved": total,
                "matched": matched,
                "queued": len(self._queued),
                "backfill": self._backfill,
            }

    # -- worker -------------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            with self._cv:
                while not self._queue and not self._stop.is_set():
                    self._cv.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                key = self._queue.popleft()
                meta = self._key_meta.get(key)
            if not meta:
                with self._lock:
                    self._queued.discard(key)
                continue
            try:
                enrichment, score = self._client.match(
                    meta["title"], meta["platform"], meta["year"],
                    meta["developer"], meta["publisher"], meta["franchise"],
                )
                self._save(key, enrichment, score)
            except Exception as exc:  # transient — leave uncached so it retries
                log.warning("enrich failed for %r: %s", meta["title"], exc)
                time.sleep(1.0)
            finally:
                with self._lock:
                    self._queued.discard(key)

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, name="enricher", daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
