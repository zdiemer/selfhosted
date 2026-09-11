"""SQLite: the sample trace, the cycle log, and the resumable machine state.

TIMESTAMPS ARE UNIX FLOATS, EVERYWHERE INSIDE.
Every comparison in detect.py is arithmetic on elapsed seconds, and the only
place a human-readable local time is needed is the body of the text message. So
the conversion happens once, at that edge, with an explicit zone. Storing ISO
strings instead would mean parsing them back on every sample, and storing local
times would put a two-hour hole in the cycle log twice a year — during which a
"quiet for 300 s" test would compare across the discontinuity.

THREE TABLES, THREE LIFETIMES:
  sample        pruned after LAUNDRY_SAMPLE_RETENTION_DAYS. It exists to draw
                the trace you pick `threshold_mg` from, not as a record.
  cycle         kept forever. It is tiny (a few rows a week) and it is the only
                evidence of whether the thing is actually working.
  machine_state one row per machine, overwritten. Persisted after EVERY sample
                so that a pod restart mid-wash resumes the open cycle instead
                of forgetting it — a restart is a helm upgrade or an evicted
                pod, both of which happen on ordinary Tuesdays.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import config
from detect import IDLE, MachineState

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sample (
    id          INTEGER PRIMARY KEY,
    device      TEXT NOT NULL,
    received_at REAL NOT NULL,
    rms_mg      REAL NOT NULL,
    peak_mg     REAL NOT NULL,
    window_ms   INTEGER,
    seq         INTEGER,
    rssi        INTEGER,
    temp_c      REAL
);
CREATE INDEX IF NOT EXISTS sample_device_time ON sample(device, received_at);

CREATE TABLE IF NOT EXISTS cycle (
    id           INTEGER PRIMARY KEY,
    device       TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL NOT NULL,
    duration_s   REAL NOT NULL,
    peak_mg      REAL NOT NULL,
    -- 0 = not sent, 1 = accepted by sms-relay. A row with notified=0 and a
    -- notify_error is the trail for "the load finished but no text arrived".
    notified     INTEGER NOT NULL DEFAULT 0,
    notify_error TEXT,
    -- Rejected as too short to be a real load. Kept rather than dropped: a pile
    -- of these means min_run_seconds or threshold_mg is wrong, and that is only
    -- visible if they were written down.
    false_start  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (device, started_at)
);
CREATE INDEX IF NOT EXISTS cycle_device_time ON cycle(device, started_at DESC);

CREATE TABLE IF NOT EXISTS machine_state (
    device           TEXT PRIMARY KEY,
    state            TEXT NOT NULL,
    active_since     REAL,
    quiet_since      REAL,
    cycle_started_at REAL,
    cycle_peak_mg    REAL NOT NULL DEFAULT 0,
    last_sample_at   REAL,
    last_rms_mg      REAL NOT NULL DEFAULT 0,
    online           INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL
);
"""

_conn: sqlite3.Connection | None = None
# One connection shared by the request handlers and the liveness sweep, so one
# lock. The writes here are single-row and the reads are indexed; contention is
# not a consideration at one sample per machine per five seconds.
_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init() -> None:
    with _lock:
        conn = connect()
        conn.executescript(SCHEMA)
        conn.commit()


def load_state(device: str) -> MachineState:
    """Rehydrate, or start fresh. `online` is deliberately NOT restored.

    A pod that has just started has heard nothing from anyone, whatever was true
    before it restarted. Coming up as offline and being corrected by the next
    sample five seconds later is honest; coming up as online and waiting for the
    sweep to notice would report a device that might be long gone.
    """
    with _lock:
        row = connect().execute(
            "SELECT * FROM machine_state WHERE device = ?", (device,)
        ).fetchone()
    if row is None:
        return MachineState(device=device)
    return MachineState(
        device=device,
        state=row["state"] or IDLE,
        active_since=row["active_since"],
        quiet_since=row["quiet_since"],
        cycle_started_at=row["cycle_started_at"],
        cycle_peak_mg=row["cycle_peak_mg"] or 0.0,
        last_sample_at=row["last_sample_at"],
        last_rms_mg=row["last_rms_mg"] or 0.0,
        online=False,
    )


def save_state(st: MachineState) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            """
            INSERT INTO machine_state
                (device, state, active_since, quiet_since, cycle_started_at,
                 cycle_peak_mg, last_sample_at, last_rms_mg, online, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device) DO UPDATE SET
                state=excluded.state,
                active_since=excluded.active_since,
                quiet_since=excluded.quiet_since,
                cycle_started_at=excluded.cycle_started_at,
                cycle_peak_mg=excluded.cycle_peak_mg,
                last_sample_at=excluded.last_sample_at,
                last_rms_mg=excluded.last_rms_mg,
                online=excluded.online,
                updated_at=excluded.updated_at
            """,
            (st.device, st.state, st.active_since, st.quiet_since,
             st.cycle_started_at, st.cycle_peak_mg, st.last_sample_at,
             st.last_rms_mg, int(st.online), time.time()),
        )
        conn.commit()


def insert_sample(
    device: str, received_at: float, rms_mg: float, peak_mg: float,
    window_ms: int | None, seq: int | None, rssi: int | None, temp_c: float | None,
) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO sample (device, received_at, rms_mg, peak_mg, window_ms,"
            " seq, rssi, temp_c) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device, received_at, rms_mg, peak_mg, window_ms, seq, rssi, temp_c),
        )
        conn.commit()


def record_cycle(
    device: str, started_at: float, ended_at: float, peak_mg: float,
    *, false_start: bool = False,
) -> int | None:
    """Write the cycle and return its id, or None if it was already written.

    The UNIQUE(device, started_at) collision is the guard against a cycle being
    recorded — and texted — twice, which is what would otherwise happen if the
    same finish were processed by a retry or a replayed sample.
    """
    with _lock:
        conn = connect()
        cur = conn.execute(
            "INSERT OR IGNORE INTO cycle (device, started_at, ended_at, duration_s,"
            " peak_mg, false_start) VALUES (?, ?, ?, ?, ?, ?)",
            (device, started_at, ended_at, ended_at - started_at, peak_mg,
             int(false_start)),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def mark_notified(cycle_id: int, ok: bool, error: str | None) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            "UPDATE cycle SET notified = ?, notify_error = ? WHERE id = ?",
            (int(ok), error, cycle_id),
        )
        conn.commit()


def recent_cycles(device: str | None = None, limit: int = 25) -> list[sqlite3.Row]:
    sql = "SELECT * FROM cycle"
    args: list = []
    if device:
        sql += " WHERE device = ?"
        args.append(device)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with _lock:
        return list(connect().execute(sql, args).fetchall())


def recent_samples(device: str, since: float, limit: int = 2000) -> list[sqlite3.Row]:
    with _lock:
        return list(connect().execute(
            "SELECT received_at, rms_mg, peak_mg FROM sample"
            " WHERE device = ? AND received_at >= ?"
            " ORDER BY received_at ASC LIMIT ?",
            (device, since, limit),
        ).fetchall())


def cycle_count(device: str) -> int:
    with _lock:
        return connect().execute(
            "SELECT COUNT(*) FROM cycle WHERE device = ? AND false_start = 0",
            (device,),
        ).fetchone()[0]


def prune_samples(older_than_days: float) -> int:
    cutoff = time.time() - older_than_days * 86400
    with _lock:
        conn = connect()
        cur = conn.execute("DELETE FROM sample WHERE received_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
