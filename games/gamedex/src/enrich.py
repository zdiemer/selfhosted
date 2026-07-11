"""Lazy, host-cached enrichment across two sources: IGDB (covers + metadata)
and HowLongToBeat (playtimes). Both are on-demand + optionally backfilled, each
with its own rate-limited worker and its own SQLite table on the PVC.

matchKeys (normalized title | platform | year) are stamped onto every served
row as `_k`; the server holds the title/platform metadata so the frontend only
sends keys. IGDB and HLTB resolve independently and are merged at read time.
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

_IGDB_LIGHT = ("igdbId", "cover", "rating", "year", "genres", "themes", "gameModes", "name")
_FACET_LIGHT = ("cover", "genres", "themes", "gameModes")
_SHEET_TITLE = {"games": "title", "completed": "game", "onOrder": "title"}
_now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


class Enricher:
    def __init__(self, igdb_client, db_path: str, backfill: bool = False, hltb_client=None):
        self._igdb = igdb_client
        self._hltb = hltb_client
        self._backfill = backfill
        self._validator = MatchValidator()
        self._key_meta: dict = {}

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        # One queue + worker per source so their rate limits stay independent.
        self._q = {"igdb": deque(), "hltb": deque()}
        self._queued = {"igdb": set(), "hltb": set()}
        self._workers = {}

        self._db_lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS enrichment(match_key TEXT PRIMARY KEY, igdb_id INTEGER,"
            " status TEXT, score INTEGER, data TEXT, updated_at TEXT, manual INTEGER DEFAULT 0)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS hltb(match_key TEXT PRIMARY KEY, status TEXT,"
            " data TEXT, updated_at TEXT)"
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(enrichment)")}
        if "manual" not in cols:
            self._db.execute("ALTER TABLE enrichment ADD COLUMN manual INTEGER DEFAULT 0")
        self._db.commit()

    # -- keys / index -------------------------------------------------------
    def key_for(self, title, platform, year) -> str:
        return f"{self._validator.normalize(title)}|{(platform or '').lower()}|{year or ''}"

    def reindex(self, parsed: dict):
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
                        "title": title, "platform": r.get("platform"), "year": r.get("releaseYear"),
                        "developer": r.get("developer"), "publisher": r.get("publisher"),
                        "franchise": r.get("franchise"),
                    }
        with self._lock:
            self._key_meta = key_meta
        if self._backfill:
            self.request(list(key_meta.keys()), front=False)
        log.info("enrich: indexed %d match keys (backfill=%s, hltb=%s)",
                 len(key_meta), self._backfill, bool(self._hltb))

    # -- db helpers ---------------------------------------------------------
    def _get(self, table, keys):
        if not keys:
            return {}
        out = {}
        with self._db_lock:
            qs = ",".join("?" * len(keys))
            for mk, status, data in self._db.execute(
                f"SELECT match_key,status,data FROM {table} WHERE match_key IN ({qs})", keys
            ):
                out[mk] = (status, json.loads(data) if data else None)
        return out

    def _status(self, table, key):
        with self._db_lock:
            row = self._db.execute(f"SELECT status FROM {table} WHERE match_key=?", (key,)).fetchone()
        return row[0] if row else None

    def _save_igdb(self, key, enrichment, score, manual=False):
        status = "matched" if enrichment else "no_match"
        with self._db_lock:
            if not manual:
                row = self._db.execute("SELECT manual FROM enrichment WHERE match_key=?", (key,)).fetchone()
                if row and row[0]:
                    return
            self._db.execute(
                "INSERT OR REPLACE INTO enrichment"
                "(match_key,igdb_id,status,score,data,updated_at,manual) VALUES(?,?,?,?,?,?,?)",
                (key, enrichment.get("igdbId") if enrichment else None, status, score,
                 json.dumps(enrichment) if enrichment else None, _now(), 1 if manual else 0),
            )
            self._db.commit()

    def _save_hltb(self, key, data):
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO hltb(match_key,status,data,updated_at) VALUES(?,?,?,?)",
                (key, "matched" if data else "no_match", json.dumps(data) if data else None, _now()),
            )
            self._db.commit()

    # -- manual override (IGDB) --------------------------------------------
    def set_override(self, key, enrichment):
        enrichment = dict(enrichment)
        enrichment["manual"] = True
        self._save_igdb(key, enrichment, enrichment.get("confidence") or 0, manual=True)
        with self._lock:
            self._queued["igdb"].discard(key)

    def clear_override(self, key):
        with self._db_lock:
            self._db.execute("DELETE FROM enrichment WHERE match_key=?", (key,))
            self._db.commit()
        self.request([key])

    # -- request / read -----------------------------------------------------
    def request(self, keys, front=True):
        with self._cv:
            for k in keys:
                if k not in self._key_meta:
                    continue
                for src, tbl in (("igdb", "enrichment"), ("hltb", "hltb")):
                    if src == "hltb" and not self._hltb:
                        continue
                    if k in self._queued[src] or self._status(tbl, k):
                        continue
                    (self._q[src].appendleft if front else self._q[src].append)(k)
                    self._queued[src].add(k)
            self._cv.notify_all()

    def _merge_hltb(self, base, hentry):
        if hentry and hentry[0] == "matched":
            h = hentry[1]
            base["hltbMain"] = h.get("main")
            base["hltbBest"] = h.get("best")
            base["hltbUrl"] = h.get("url")

    def get_light(self, keys):
        igdb = self._get("enrichment", keys)
        hltb = self._get("hltb", keys)
        items, pending = {}, []
        for k in keys:
            e = igdb.get(k)
            base = {f: e[1].get(f) for f in _IGDB_LIGHT} if e and e[0] == "matched" else {}
            self._merge_hltb(base, hltb.get(k))
            if base:
                items[k] = base
            if e is None or (self._hltb and hltb.get(k) is None):
                pending.append(k)
        return items, pending

    def get_all_light(self):
        out = {}
        with self._db_lock:
            for mk, data in self._db.execute("SELECT match_key,data FROM enrichment WHERE status='matched'"):
                if data:
                    d = json.loads(data)
                    out[mk] = {f: d.get(f) for f in _FACET_LIGHT}
            for mk, data in self._db.execute("SELECT match_key,data FROM hltb WHERE status='matched'"):
                if data:
                    h = json.loads(data)
                    out.setdefault(mk, {}).update(
                        {"hltbMain": h.get("main"), "hltbBest": h.get("best"), "hltbUrl": h.get("url")})
        return out

    def get_detail(self, key):
        e = self._get("enrichment", [key]).get(key)
        if e:
            return e[0], (e[1] if e[0] == "matched" else None)
        self.request([key])
        return "pending", None

    def get_hltb(self, key):
        h = self._get("hltb", [key]).get(key)
        return h[1] if (h and h[0] == "matched") else None

    def stats(self):
        with self._db_lock:
            im = self._db.execute("SELECT COUNT(*) FROM enrichment WHERE status='matched'").fetchone()[0]
            ir = self._db.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0]
            hm = self._db.execute("SELECT COUNT(*) FROM hltb WHERE status='matched'").fetchone()[0]
            hr = self._db.execute("SELECT COUNT(*) FROM hltb").fetchone()[0]
        with self._lock:
            total = len(self._key_meta)
            iq, hq = len(self._queued["igdb"]), len(self._queued["hltb"])
        complete = ir >= total and (not self._hltb or hr >= total)
        return {"total": total, "resolved": ir, "matched": im, "queued": iq,
                "hltb": {"resolved": hr, "matched": hm, "queued": hq},
                "backfill": self._backfill, "complete": complete}

    @property
    def ready(self):
        return bool(self._key_meta)

    # -- workers ------------------------------------------------------------
    def _loop(self, src):
        while not self._stop.is_set():
            with self._cv:
                while not self._q[src] and not self._stop.is_set():
                    self._cv.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                key = self._q[src].popleft()
                meta = self._key_meta.get(key)
            if not meta:
                with self._lock:
                    self._queued[src].discard(key)
                continue
            try:
                if src == "igdb":
                    enrichment, score = self._igdb.match(
                        meta["title"], meta["platform"], meta["year"],
                        meta["developer"], meta["publisher"], meta["franchise"])
                    self._save_igdb(key, enrichment, score)
                else:
                    self._save_hltb(key, self._hltb.match(meta["title"], meta["platform"], meta["year"]))
            except Exception as exc:
                log.warning("%s enrich failed for %r: %s", src, meta["title"], exc)
                time.sleep(1.0)
            finally:
                with self._lock:
                    self._queued[src].discard(key)

    def start(self):
        for src in ("igdb", "hltb"):
            if src == "hltb" and not self._hltb:
                continue
            t = threading.Thread(target=self._loop, args=(src,), name=f"enricher-{src}", daemon=True)
            self._workers[src] = t
            t.start()

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
