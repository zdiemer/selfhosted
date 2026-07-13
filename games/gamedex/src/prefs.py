"""Server-side prefs: saved views and custom challenges, kept off the browser.

These used to live in localStorage, which means they exist on exactly one browser
on one machine — a challenge you set up on the desktop is invisible on the phone,
and clearing site data throws away work.

A tiny key/value table on the same PVC the enrichment cache uses. No schema per
pref type: the client owns the shape, the server just stores the JSON. That way a
new kind of pref needs no backend change.

No auth, deliberately (personal instance; accounts can come later if they ever
matter). The browser keeps a localStorage mirror so the app still works offline —
it's a PWA — and so a failed write is never a lost edit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger("gamedex.prefs")

# Anything not on this list is refused, so a stray key can't fill the disk.
KEYS = {"views", "challenges", "picross"}   # picross: the daily streak, so it follows you between devices
MAX_BYTES = 256 * 1024      # a pref is a small list of definitions, not a payload


class Prefs:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS prefs("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        self._db.commit()

    def get_all(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM prefs").fetchall()
        out = {}
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except Exception:
                continue          # a corrupt row must not take the whole response down
        return out

    def put(self, key: str, value) -> None:
        if key not in KEYS:
            raise ValueError(f"unknown pref key: {key}")
        blob = json.dumps(value, separators=(",", ":"))
        if len(blob) > MAX_BYTES:
            raise ValueError("pref too large")
        with self._lock:
            self._db.execute(
                "INSERT INTO prefs(key, value, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, blob, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self._db.commit()
        log.info("prefs: saved %s (%d bytes)", key, len(blob))
