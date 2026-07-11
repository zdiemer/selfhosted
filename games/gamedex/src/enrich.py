"""Lazy, host-cached enrichment across multiple sources: IGDB (covers + metadata,
primary, supports manual override) plus any number of secondary sources — HLTB
(playtimes) and Metacritic (critic scores). Each source is on-demand + optionally
backfilled, with its own rate-limited worker, queue, and SQLite table on the PVC.

matchKeys (normalized title | platform | year) are stamped onto every served row
as `_k`; sources resolve independently and are merged at read time.
"""

from __future__ import annotations

import json
import os
import re
import logging
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

from match_validator import MatchValidator

log = logging.getLogger("gamedex.enrich")

_IGDB_LIGHT = ("igdbId", "cover", "coverUrl", "source", "rating", "year", "genres", "themes", "gameModes",
               "name", "stores")
# igdbId/source let the UI tell IGDB matches from fallback (IGN/GameSpot/Steam)
# ones, and spot games with no metadata at all.
_FACET_LIGHT = ("cover", "coverUrl", "genres", "themes", "gameModes", "userRating",
                "igdbId", "source", "stores")


def _light_relations(rec):
    """Just enough of the graph for the grid: what KIND of entry this is, and
    the id of its parent — the grouped view folds ports into the game they're a
    port OF, which needs the id, not just the name."""
    rel = (rec or {}).get("relations") or {}
    if not rel:
        return None
    parent = rel.get("parent") or {}
    return {
        "type": rel.get("gameTypeLabel"),
        "parent": parent.get("name"),
        "parentId": parent.get("id"),
    }


def _light_video(rec):
    """The first trailer id, for the hover-to-play preview on the grid.

    Derived rather than stored: `videos` has been in every enrichment record from
    the start, so pulling the id out here means the whole library gets previews
    with no re-enrichment at all.
    """
    vids = (rec or {}).get("videos") or []
    return vids[0].get("id") if vids and vids[0].get("id") else None
# Light fields each secondary source contributes to the cover/facet map.
# Sources keyed on the Steam appid, which lives in the IGDB *enrichment* record
# rather than the sheet — so their worker has to wait for IGDB to resolve first.
_NEEDS_APPID = {"steamx"}

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
    "steamx": lambda d: {"deck": d.get("deck"), "protonTier": d.get("protonTier"),
                         "steamReview": d.get("reviewScore"), "owners": d.get("owners")},
    "speedrun": lambda d: {"wrTime": d.get("wrTime"), "wrSeconds": d.get("wrSeconds")},
    "guides": lambda d: {"guideUrl": d.get("url")},
}

# Not every source applies to every game. These gates keep the queues honest:
# an arcade scraper has nothing to say about a Switch game, and asking anyway
# would just burn rate limit and write thousands of no_match rows.
_SOURCE_GATE = {
    "gameye": lambda m: m.get("owned") and (m.get("format") or "").lower() == "physical",
    "arcadedb": lambda m: bool(m.get("mameRomset")),
    "thumby": lambda m: m.get("platform") in ("Thumby", "Thumby Color"),
    "vndb": lambda m: m.get("genre") in ("Visual Novel", "Adventure"),
    # Gated on platform, not on the appid: the appid isn't known until IGDB has
    # resolved, and gates run at queue time. The worker drops the ones that turn
    # out to have no appid.
    "steamx": lambda m: m.get("platform") in ("PC", "Mac", "Linux"),
}
_SHEET_TITLE = {"games": "title", "completed": "game", "onOrder": "title"}
_now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
VALUE_RESCRAPE_DAYS = int(os.environ.get("VALUE_RESCRAPE_DAYS", "7"))
# Bump when the shape or the source map of `stores` changes — the backfill
# rebuilds every record instead of skipping the ones that already have a value.
STORES_VERSION = "4"
RELATIONS_VERSION = "2"


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
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS value_history("
            " day TEXT PRIMARY KEY, total REAL, games INTEGER, priced INTEGER)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        self._db.commit()

    # -- collection value over time -----------------------------------------
    # Re-scrape prices every N days (the snapshot itself is daily and free).
    # GameEye only ever tells us today's price, so a trend has to be recorded as
    # it happens. One row per day; the first won't be interesting, the hundredth
    # will be.
    _COND_KEY = {"complete": "priceCib", "cib": "priceCib", "loose": "priceLoose", "new": "priceNew"}

    @staticmethod
    def _copies(notes):
        """"Two copies owned" -> 2. Mirrors quantityFromNotes() in the UI."""
        if not notes:
            return 1
        m = re.search(r"(\d+)\s+cop(?:y|ies)", str(notes), re.I)
        if m:
            return int(m.group(1))
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8}
        m = re.search(r"\b(one|two|three|four|five|six|seven|eight)\s+cop(?:y|ies)", str(notes), re.I)
        return words.get(m.group(1).lower(), 1) if m else 1

    def snapshot_value(self):
        """Record today's total collection value (owned physical games)."""
        gate = _SOURCE_GATE["gameye"]
        with self._lock:
            owned = {k: m for k, m in self._key_meta.items() if gate(m)}
        if not owned:
            return
        prices = self._get("gameye", list(owned))
        total, priced = 0.0, 0
        for key, meta in owned.items():
            entry = prices.get(key)
            if not entry or entry[0] != "matched" or not entry[1]:
                continue
            field = self._COND_KEY.get(str(meta.get("condition") or "").lower(), "priceLoose")
            price = entry[1].get(field) or entry[1].get("priceLoose")
            if price is None:
                continue
            total += price * self._copies(meta.get("notes"))
            priced += 1
        day = datetime.now(timezone.utc).date().isoformat()
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO value_history(day,total,games,priced) VALUES(?,?,?,?)",
                (day, round(total, 2), len(owned), priced),
            )
            self._db.commit()
        log.info("value snapshot %s: $%s across %d priced games", day, f"{total:,.2f}", priced)

    def value_history(self):
        with self._db_lock:
            rows = self._db.execute(
                "SELECT day,total,games,priced FROM value_history ORDER BY day"
            ).fetchall()
        return [{"day": d, "total": t, "games": g, "priced": p} for d, t, g, p in rows]

    def _kv_get(self, k):
        with self._db_lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def _kv_set(self, k, v):
        with self._db_lock:
            self._db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
            self._db.commit()

    def _days_since_scrape(self):
        last = self._kv_get("gameye_scraped")
        if not last:
            return 10 ** 6                      # never scraped: do it now
        try:
            then = datetime.fromisoformat(last).date()
        except ValueError:
            return 10 ** 6
        return (datetime.now(timezone.utc).date() - then).days

    def refresh_source(self, src):
        """Re-scrape every game a source applies to, ignoring cached results.

        request() deliberately skips keys that already resolved — that's what
        stops the backfill re-doing work. For prices we want the opposite: a
        cached price is a stale price. Manual overrides are still protected, by
        the manual=1 guard in _save_secondary.
        """
        if src not in self._secondary:
            return 0
        gate = _SOURCE_GATE.get(src)
        with self._cv:
            n = 0
            for key, meta in self._key_meta.items():
                if gate and not gate(meta):
                    continue
                if key in self._queued[src]:
                    continue
                self._q[src].append(key)
                self._queued[src].add(key)
                n += 1
            self._cv.notify_all()
        log.info("%s: re-queued %d games for a fresh scrape", src, n)
        return n

    def _drain(self, src, timeout):
        """Block until a source's queue empties (or we run out of patience)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._queued[src]:
                    return True
            if self._stop.wait(30):
                return False
        return False

    def _value_loop(self):
        """Snapshot once a day.

        Waits for the first reindex: start() runs before the poller has fetched
        the spreadsheet, so at boot there are no keys to price and an eager
        snapshot would record nothing and then sleep for 24 hours.
        """
        while not self._stop.is_set():
            with self._lock:
                ready = bool(self._key_meta)
            if not ready:
                if self._stop.wait(20):
                    return
                continue
            try:
                # Prices are re-scraped weekly: GameEye allows 500/hr and there
                # are ~2k owned physical games, so a full pass is ~4h — fine
                # weekly, wasteful daily, and market values don't move that fast.
                # The snapshot itself runs daily regardless, so buying or selling
                # something shows up the next day even between scrapes.
                #
                # The "last scraped" date lives in the DB, not in memory: this
                # process restarts on every deploy, and an in-memory counter
                # would kick off a 4-hour scrape each time.
                if self._days_since_scrape() >= VALUE_RESCRAPE_DAYS:
                    if self.refresh_source("gameye"):
                        self._drain("gameye", timeout=8 * 3600)
                    self._kv_set("gameye_scraped", datetime.now(timezone.utc).date().isoformat())
                self.snapshot_value()
            except Exception as exc:
                log.warning("value snapshot failed: %s", exc)
            if self._stop.wait(24 * 3600):
                return

    def _table(self, src):
        return "enrichment" if src == "igdb" else src

    def appid_for(self, key):
        """The Steam appid from a key's primary metadata record, if it has one."""
        row = self._get("enrichment", [key]).get(key)
        if not row or row[0] != "matched" or not row[1]:
            return None
        st = (row[1].get("stores") or {}).get("steam")
        if not st:
            return None
        return st.get("id") if isinstance(st, dict) else str(st)

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
                        # For the daily value snapshot: price depends on the
                        # copy's condition, and the notes say how many you own.
                        "condition": r.get("condition"), "notes": r.get("notes"),
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
            if e and e[0] == "matched":
                base["video"] = _light_video(e[1])
                base["rel"] = _light_relations(e[1])
            for src, extract in _SECONDARY_LIGHT.items():
                entry = sec.get(src, {}).get(k)
                if entry and entry[0] == "matched":
                    base.update(extract(entry[1]))
            if base:
                items[k] = base
            if e is None or any(sec[src].get(k) is None for src in self._secondary):
                pending.append(k)
        return items, pending

    def all_records(self):
        """{match_key: igdb record} for every matched game. Used by the recommender,
        which needs the `similar_games` list we've been storing all along."""
        out = {}
        with self._db_lock:
            for mk, data in self._db.execute(
                    "SELECT match_key,data FROM enrichment WHERE status='matched' AND data IS NOT NULL"):
                try:
                    out[mk] = json.loads(data)
                except Exception:
                    continue
        return out

    @property
    def normalize(self):
        return self._validator.normalize

    def get_all_light(self):
        out = {}
        with self._db_lock:
            for mk, data in self._db.execute("SELECT match_key,data FROM enrichment WHERE status='matched'"):
                if data:
                    d = json.loads(data)
                    out[mk] = {f: d.get(f) for f in _FACET_LIGHT}
                    out[mk]["video"] = _light_video(d)
                    out[mk]["rel"] = _light_relations(d)
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
            requeued = False
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
                elif src in _NEEDS_APPID:
                    appid = self.appid_for(key)
                    if appid is None:
                        if self._status("enrichment", key) is None:
                            # IGDB hasn't got to this game yet. Put it back and
                            # come round again rather than recording a no_match
                            # we'd never revisit.
                            with self._lock:
                                self._q[src].append(key)
                            requeued = True
                            time.sleep(0.05)
                            continue
                        # IGDB resolved and there's no Steam id — nothing to ask.
                        self._save_secondary(src, key, None)
                        continue
                    client = self._secondary[src]
                    self._save_secondary(src, key, client.match_meta({**meta, "steamAppId": appid}))
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
                if not requeued:
                    with self._lock:
                        self._queued[src].discard(key)

    def backfill_stores(self):
        """One-off: add `stores` to records matched before we asked IGDB for them.

        Cheap — fetch-by-id batches 500 games per call, so the whole library is
        ~30 requests rather than 14.5k searches. Steam-sourced fallback records
        get theirs parsed straight out of the stored URL, no network at all.
        """
        with self._db_lock:
            rows = self._db.execute(
                "SELECT match_key, igdb_id, data FROM enrichment WHERE status='matched' AND data IS NOT NULL"
            ).fetchall()

        stale = self._kv_get("stores_version") != STORES_VERSION
        need, steam_fixed = {}, 0
        for key, igdb_id, raw in rows:
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("stores") and not stale:
                continue
            m = re.search(r"/app/(\d+)", rec.get("url") or "")
            if (rec.get("source") == "Steam") and m:
                rec["stores"] = {"steam": {"id": m.group(1),
                                           "url": f"https://store.steampowered.com/app/{m.group(1)}/"}}
                with self._db_lock:
                    self._db.execute("UPDATE enrichment SET data=? WHERE match_key=?",
                                     (json.dumps(rec), key))
                steam_fixed += 1
            elif igdb_id:
                need.setdefault(int(igdb_id), []).append(key)
        if steam_fixed:
            with self._db_lock:
                self._db.commit()

        if not need or not self._igdb.configured:
            log.info("stores backfill: %d from Steam URLs, nothing to fetch", steam_fixed)
            return
        log.info("stores backfill: fetching storefront ids for %d games", len(need))
        found = 0
        try:
            stores = self._igdb.stores_for(list(need))
        except Exception as exc:
            log.warning("stores backfill failed: %s", exc)
            return
        with self._db_lock:
            for gid, st in stores.items():
                # Every row sharing this IGDB id — the same game on PS5 and PC is
                # two rows and both want the appid.
                for key in need.get(gid, []):
                    row = self._db.execute("SELECT data FROM enrichment WHERE match_key=?", (key,)).fetchone()
                    if not row or not row[0]:
                        continue
                    rec = json.loads(row[0])
                    rec["stores"] = st
                    self._db.execute("UPDATE enrichment SET data=? WHERE match_key=?", (json.dumps(rec), key))
                    found += 1
            self._db.commit()
        self._kv_set("stores_version", STORES_VERSION)
        log.info("stores backfill: %d games got storefront ids (+%d from Steam URLs)", found, steam_fixed)

    def backfill_relations(self):
        """Add IGDB's relationship graph (parent/dlcs/remakes/ports/…) to matched
        records. Fetch-by-id, 500 per request — the whole library in ~30 calls."""
        if not self._igdb.configured:
            return
        stale = self._kv_get("relations_version") != RELATIONS_VERSION
        with self._db_lock:
            rows = self._db.execute(
                "SELECT match_key, igdb_id, data FROM enrichment"
                " WHERE status='matched' AND igdb_id IS NOT NULL AND data IS NOT NULL"
            ).fetchall()
        need = {}
        for key, igdb_id, raw in rows:
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("relations") and not stale:
                continue
            need.setdefault(int(igdb_id), []).append(key)
        if not need:
            return
        log.info("relations backfill: fetching the graph for %d games", len(need))
        found = 0
        try:
            rels = self._igdb.relations_for(list(need))
        except Exception as exc:
            log.warning("relations backfill failed: %s", exc)
            return
        with self._db_lock:
            for gid, rel in rels.items():
                for key in need.get(gid, []):
                    row = self._db.execute("SELECT data FROM enrichment WHERE match_key=?", (key,)).fetchone()
                    if not row or not row[0]:
                        continue
                    rec = json.loads(row[0])
                    rec["relations"] = rel
                    self._db.execute("UPDATE enrichment SET data=? WHERE match_key=?", (json.dumps(rec), key))
                    found += 1
            self._db.commit()
        self._kv_set("relations_version", RELATIONS_VERSION)
        log.info("relations backfill: %d games linked", found)

    def start(self):
        for src in self._sources:
            t = threading.Thread(target=self._loop, args=(src,), name=f"enricher-{src}", daemon=True)
            self._workers[src] = t
            t.start()
        threading.Thread(target=self.backfill_stores, name="stores-backfill", daemon=True).start()
        threading.Thread(target=self.backfill_relations, name="relations-backfill", daemon=True).start()
        if "gameye" in self._secondary:
            threading.Thread(target=self._value_loop, name="value-history", daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
