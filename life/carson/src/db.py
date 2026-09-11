"""SQLite schema and access for carson.

One file on a PVC, WAL mode, opened by two processes: the web/API Deployment
(reads and writes) and the reminder CronJob (reads, and writes reminder_log).
WAL is what makes that safe — a writer does not block readers — but it is still
ONE writer at a time, so anything long-running must not hold a transaction open.

Schema notes that are load-bearing rather than incidental:

* `handle` is separate from `person` because the handle->person join is the
  spine of the whole system: an email address, a phone number and an iMessage
  id all have to land on one contact. Unmatched handles are NOT guessed at —
  they accumulate in the triage list (see `unmatched_handle`) for a human.

* `important_date.year` is nullable on purpose. Half the birthdays anyone knows
  come without a birth year, and a schema that demands one invites a fake.
  Anything computing an age has to cope with NULL.

* `reminder_log` exists so the ladder is idempotent per (date, year, stage).
  Without it a CronJob that runs twice — a retry, a manual kick, a clock
  change — texts twice, and a reminder system that cries wolf gets muted.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.environ.get("CARSON_DB", "/data/carson.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS person (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    -- close | family | keep_in_touch | none. Drives the nudge engine in phase 3;
    -- stored from phase 1 so the contact list does not need re-editing later.
    cadence_tier  TEXT NOT NULL DEFAULT 'none',
    -- Gate for tier-3 full-content extraction (phase 6). Default off, always.
    enrich_optin  INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handle (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    -- email | phone | imessage
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX IF NOT EXISTS handle_person ON handle(person_id);

CREATE TABLE IF NOT EXISTS important_date (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    -- birthday | anniversary | custom
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    month      INTEGER NOT NULL,
    day        INTEGER NOT NULL,
    -- NULL when the year is unknown, which is the common case for birthdays.
    year       INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS important_date_person ON important_date(person_id);
CREATE INDEX IF NOT EXISTS important_date_md ON important_date(month, day);

-- What was actually given, so the T-21 research run can avoid repeats.
CREATE TABLE IF NOT EXISTS gift_history (
    id                INTEGER PRIMARY KEY,
    important_date_id INTEGER NOT NULL REFERENCES important_date(id) ON DELETE CASCADE,
    year              INTEGER NOT NULL,
    description       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- Shortlist produced by the T-21 Claude run, referenced again at T-7.
CREATE TABLE IF NOT EXISTS gift_idea (
    id                INTEGER PRIMARY KEY,
    important_date_id INTEGER NOT NULL REFERENCES important_date(id) ON DELETE CASCADE,
    year              INTEGER NOT NULL,
    title             TEXT NOT NULL,
    url               TEXT NOT NULL DEFAULT '',
    price             TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS gift_idea_date_year ON gift_idea(important_date_id, year);

CREATE TABLE IF NOT EXISTS interaction (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    -- email | imessage | calendar | manual
    channel    TEXT NOT NULL,
    -- in | out
    direction  TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS interaction_person_ts ON interaction(person_id, ts DESC);

CREATE TABLE IF NOT EXISTS note (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    text       TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS note_person_ts ON note(person_id, ts DESC);

CREATE TABLE IF NOT EXISTS todo (
    id             INTEGER PRIMARY KEY,
    text           TEXT NOT NULL,
    -- open | done | dropped
    status         TEXT NOT NULL DEFAULT 'open',
    due            TEXT,
    person_id      INTEGER REFERENCES person(id) ON DELETE SET NULL,
    -- The quote that produced it, so a nag can show its work.
    source_snippet TEXT NOT NULL DEFAULT '',
    source_ref     TEXT NOT NULL DEFAULT '',
    -- extracted | manual | generated
    origin         TEXT NOT NULL DEFAULT 'manual',
    nag_state      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS todo_status_due ON todo(status, due);

CREATE TABLE IF NOT EXISTS proposal (
    id          INTEGER PRIMARY KEY,
    -- event | todo
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0,
    -- pending | accepted | ignored
    status      TEXT NOT NULL DEFAULT 'pending',
    source_ref  TEXT NOT NULL DEFAULT '',
    -- The Y1/N1 code the SMS round-trip matches replies on. Unique among
    -- pending proposals only; reused once a proposal is resolved.
    short_code  TEXT,
    reply_text  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposal_status ON proposal(status);

CREATE TABLE IF NOT EXISTS event (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    summary     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at   TEXT NOT NULL,
    ends_at     TEXT,
    all_day     INTEGER NOT NULL DEFAULT 0,
    person_id   INTEGER REFERENCES person(id) ON DELETE SET NULL,
    -- accepted | generated
    origin      TEXT NOT NULL DEFAULT 'accepted',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_starts ON event(starts_at);

CREATE TABLE IF NOT EXISTS escalation (
    id         INTEGER PRIMARY KEY,
    raw        TEXT NOT NULL,
    why        TEXT NOT NULL DEFAULT '',
    -- pending | resolved
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

-- Handles seen in ingest that match no person. A triage list, not a guess.
CREATE TABLE IF NOT EXISTS unmatched_handle (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS source_cursor (
    source     TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per reminder actually sent. The UNIQUE is the idempotency guard.
CREATE TABLE IF NOT EXISTS reminder_log (
    id                INTEGER PRIMARY KEY,
    important_date_id INTEGER NOT NULL REFERENCES important_date(id) ON DELETE CASCADE,
    year              INTEGER NOT NULL,
    -- t21 | t7 | t1 | t0
    stage             TEXT NOT NULL,
    sent_at           TEXT NOT NULL,
    UNIQUE (important_date_id, year, stage)
);
"""


def now() -> str:
    """UTC, ISO-8601, second resolution. Every timestamp column uses this."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    if readonly:
        # A missing file in read-only mode raises rather than silently creating
        # an empty database that then looks like "you have no contacts".
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | None = None) -> None:
    """Create the schema. Safe to run on every start — all DDL is IF NOT EXISTS."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
