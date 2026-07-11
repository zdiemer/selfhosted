"""Lazy, host-cached enrichment across multiple sources: IGDB (covers + metadata,
primary, supports manual override) plus any number of secondary sources — HLTB
(playtimes) and Metacritic (critic scores). Each source is on-demand + optionally
backfilled, with its own rate-limited worker, queue, and SQLite table on the PVC.

matchKeys (normalized title | platform | year) are stamped onto every served row
as `_k`; sources resolve independently and are merged at read time.
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

_IGDB_LIGHT = ("igdbId", "cover", "coverUrl", "source", "rating", "year", "genres", "themes", "gameModes", "name")
# igdbId/source let the UI tell IGDB matches from fallback (IGN/GameSpot/Steam)
# ones, and spot games with no metadata at all.
_FACET_LIGHT = ("cover", "coverUrl", "genres", "themes", "gameModes", "userRating",
                "igdbId", "source")
# Light fields each secondary source contributes to the cover/facet map.
_SECONDARY_LIGHT = {
    "hltb": lambda d: {"hltbMain": d.get("main"), "hltbBest": d.get("best"), "hltbUrl": d.get("url")},
    "metacritic": lambda d: {"metascore": d.get("metascore"), "metaUrl": d.get("url")},
    "gameye": lambda d: {"geLoose": d.get("priceLoose"), "geCib": d.get("priceCib"),
                         "geNew": d.get("priceNew"), "geUrl": d.get("url")},
    # Arcade art doubles as a cover for games IGDB has no box art for.
    "arcadedb": lambda d: {"adbCover": d.get("cabinet") or d.get("flyer") or d.get("titleScreen"),
                           "adbPlayers": d.get("playersDetail"), "adbOrientation": d.get("orientation"),
                           "adbUrl": d.get("url")},
    "vndb": lambda d: {"vnRating": d.get("rating"), "vnHours": d.get("hours"),
                       "vnCover": d.get("cover"), "vnUrl": d.get("url")},
    "vgchartz": lambda d: {"units": d.get("units"), "vgcUrl": d.get("url")},
    "thumby": lambda d: {"thumbyUrl": d.get("url"), "thumbyCover": d.get("cover")},
}

# Not every source applies to every game. These gates keep the queues honest:
# an arcade scraper has nothing to say about a Switch game, and asking anyway
# would just burn rate limit and write thousands of no_match rows.
_SOURCE_GATE = {
    "gameye": lambda m: m.get("owned") and (m.get("format") or "").lower() == "physical",
    "arcadedb": lambda m: bool(m.get("mameRomset")),
    "thumby": lambda m: m.get("platform") in ("Thumby", "Thumby Color"),
    "vndb": lambda m: m.get("genre") in ("Visual Novel", "Adventure"),
}
_SHEET_TITLE = {"games": "title", "completed": "game", "onOrder": "title"}
_now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


class Enricher:
    def __init__(self, igdb_client, db_path: str, backfill: bool = False,
                 secondary: dict = None, fallback=None):
        self._igdb = igdb_client
        self._secondary = dict(secondary or {})     # name -> client
        self._fallback = fallback                   # tried when IGDB misses
        self._backfill = backfill
        self._validator = MatchValidator()
        self._key_meta: dict = {}
        self._sources = ["igdb"] + list(self._secondary)

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._q = {s: deque() for s in self._sources}
        self._queued = {s: set() for s in self._sources}
        self._workers = {}

        self._db_lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS enrichment(match_key TEXT PRIMARY KEY, igdb_id INTEGER,"
            " status TEXT, score INTEGER, data TEXT, updated_at TEXT, manual INTEGER DEFAULT 0)"
        )
        for src in self._secondary:
            self._db.execute(
                f"CREATE TABLE IF NOT EXISTS {src}(match_key TEXT PRIMARY KEY, status TEXT,"
                f" data TEXT, updated_at TEXT, manual INTEGER DEFAULT 0)"
            )
            if "manual" not in {r[1] for r in self._db.execute(f"PRAGMA table_info({src})")}:
                self._db.execute(f"ALTER TABLE {src} ADD COLUMN manual INTEGER DEFAULT 0")
        if "manual" not in {r[1] for r in self._db.execute("PRAGMA table_info(enrichment)")}:
            self._db.execute("ALTER TABLE enrichment ADD COLUMN manual INTEGER DEFAULT 0")
        self._db.commit()

    def _table(self, src):
        return "enrichment" if src == "igdb" else src

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
                        # For the per-source gates in _SOURCE_GATE, and for
                        # ArcadeDB, which looks up by romset rather than title.
                        "owned": bool(r.get("owned")), "format": r.get("format"),
                        "mameRomset": r.get("mameRomset"), "genre": r.get("genre"),
                    }
        with self._lock:
            self._key_meta = key_meta
        # Give previously-unmatched games a shot at the fallback sources by
        # clearing their no_match rows so the backfill reprocesses them.
        if self._fallback:
            with self._db_lock:
                self._db.execute("DELETE FROM enrichment WHERE status='no_match'")
                self._db.commit()
        if self._backfill:
            self.request(list(key_meta.keys()), front=False)
        log.info("enrich: indexed %d match keys (backfill=%s, sources=%s)",
                 len(key_meta), self._backfill, self._sources)

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

    def _save_secondary(self, src, key, data, manual=False):
        with self._db_lock:
            if not manual:   # don't let an auto result clobber a manual override
                row = self._db.execute(f"SELECT manual FROM {src} WHERE match_key=?", (key,)).fetchone()
                if row and row[0]:
                    return
            self._db.execute(
                f"INSERT OR REPLACE INTO {src}(match_key,status,data,updated_at,manual) VALUES(?,?,?,?,?)",
                (key, "matched" if data else "no_match", json.dumps(data) if data else None,
                 _now(), 1 if manual else 0),
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

    def meta_for(self, key):
        return self._key_meta.get(key)

    def set_source_override(self, source, key, record):
        """Pin any source (igdb or a secondary) to a manually chosen record."""
        if source == "igdb":
            self.set_override(key, record)
        elif source in self._secondary:
            rec = dict(record)
            rec["manual"] = True
            self._save_secondary(source, key, rec, manual=True)
            with self._lock:
                self._queued[source].discard(key)

    def remove_source(self, source, key):
        """Pin a source as 'no match' (manual), so auto-matching won't re-fill it.
        Different from clear_source_override, which hands the key back to auto."""
        if source == "igdb":
            self._save_igdb(key, None, 0, manual=True)
        elif source in self._secondary:
            self._save_secondary(source, key, None, manual=True)
        else:
            return
        with self._lock:
            self._queued[source].discard(key)

    def clear_source_override(self, source, key):
        if source == "igdb":
            self.clear_override(key)
        elif source in self._secondary:
            with self._db_lock:
                self._db.execute(f"DELETE FROM {source} WHERE match_key=?", (key,))
                self._db.commit()
            self.request([key])

    # -- request / read -----------------------------------------------------
    def request(self, keys, front=True):
        with self._cv:
            for k in keys:
                if k not in self._key_meta:
                    continue
                meta = self._key_meta[k]
                for src in self._sources:
                    gate = _SOURCE_GATE.get(src)
                    if gate and not gate(meta):
                        continue
                    if self._status(self._table(src), k):
                        continue
                    q = self._q[src]
                    if k in self._queued[src]:
                        # Already queued (e.g. by backfill at the back) — promote
                        # an on-demand request to the front so it resolves now.
                        if front:
                            try:
                                q.remove(k)
                            except ValueError:
                                pass
                            q.appendleft(k)
                        continue
                    (q.appendleft if front else q.append)(k)
                    self._queued[src].add(k)
            self._cv.notify_all()

    def _secondary_light(self, key):
        out = {}
        for src, extract in _SECONDARY_LIGHT.items():
            if src not in self._secondary:
                continue
            entry = self._get(src, [key]).get(key)
            if entry and entry[0] == "matched":
                out.update(extract(entry[1]))
        return out

    def get_light(self, keys):
        igdb = self._get("enrichment", keys)
        sec = {src: self._get(src, keys) for src in self._secondary}
        items, pending = {}, []
        for k in keys:
            e = igdb.get(k)
            base = {f: e[1].get(f) for f in _IGDB_LIGHT} if e and e[0] == "matched" else {}
            for src, extract in _SECONDARY_LIGHT.items():
                entry = sec.get(src, {}).get(k)
                if entry and entry[0] == "matched":
                    base.update(extract(entry[1]))
            if base:
                items[k] = base
            if e is None or any(sec[src].get(k) is None for src in self._secondary):
                pending.append(k)
        return items, pending

    def get_all_light(self):
        out = {}
        with self._db_lock:
            for mk, data in self._db.execute("SELECT match_key,data FROM enrichment WHERE status='matched'"):
                if data:
                    d = json.loads(data)
                    out[mk] = {f: d.get(f) for f in _FACET_LIGHT}
            for src, extract in _SECONDARY_LIGHT.items():
                if src not in self._secondary:
                    continue
                for mk, data in self._db.execute(f"SELECT match_key,data FROM {src} WHERE status='matched'"):
                    if data:
                        out.setdefault(mk, {}).update(extract(json.loads(data)))
        return out

    def get_detail(self, key):
        e = self._get("enrichment", [key]).get(key)
        if e:
            return e[0], (e[1] if e[0] == "matched" else None)
        self.request([key])
        return "pending", None

    def get_secondary(self, src, key):
        if src not in self._secondary:
            return None
        entry = self._get(src, [key]).get(key)
        return entry[1] if (entry and entry[0] == "matched") else None

    def stats(self):
        with self._db_lock:
            im = self._db.execute("SELECT COUNT(*) FROM enrichment WHERE status='matched'").fetchone()[0]
            ir = self._db.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0]
            sec = {}
            for src in self._secondary:
                m = self._db.execute(f"SELECT COUNT(*) FROM {src} WHERE status='matched'").fetchone()[0]
                r = self._db.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
                sec[src] = {"matched": m, "resolved": r}
        with self._lock:
            total = len(self._key_meta)
            iq = len(self._queued["igdb"])
            for src in self._secondary:
                sec[src]["queued"] = len(self._queued[src])
                # A gated source is only ever asked about the games it applies to,
                # so measure it against THAT total — otherwise ArcadeDB (584 of
                # 14.9k games) would sit at 4% forever and never read as complete.
                gate = _SOURCE_GATE.get(src)
                sec[src]["total"] = (
                    sum(1 for m in self._key_meta.values() if gate(m)) if gate else total
                )
        complete = ir >= total and all(
            sec[s]["resolved"] >= sec[s]["total"] for s in self._secondary)
        return {"total": total, "resolved": ir, "matched": im, "queued": iq,
                "sources": sec, "backfill": self._backfill, "complete": complete}

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
                    if enrichment is None and self._fallback:   # IGDB miss → fallbacks
                        fb = self._fallback.match(meta["title"], meta["platform"], meta["year"])
                        if fb:
                            enrichment, score = fb, fb.get("confidence") or 0
                    self._save_igdb(key, enrichment, score)
                else:
                    client = self._secondary[src]
                    # ArcadeDB keys on the MAME romset and Thumby/VNDB on
                    # platform/genre — a bare (title, platform, year) isn't enough.
                    rec = (client.match_meta(meta) if hasattr(client, "match_meta")
                           else client.match(meta["title"], meta["platform"], meta["year"]))
                    self._save_secondary(src, key, rec)
            except Exception as exc:
                log.warning("%s enrich failed for %r: %s", src, meta["title"], exc)
                time.sleep(1.0)
            finally:
                with self._lock:
                    self._queued[src].discard(key)

    def start(self):
        for src in self._sources:
            t = threading.Thread(target=self._loop, args=(src,), name=f"enricher-{src}", daemon=True)
            self._workers[src] = t
            t.start()

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
