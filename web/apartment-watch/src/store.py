"""SQLite state: what we've seen, what we've already texted about, source health.

Two ideas carry most of the weight here.

**Every listing is stored, matching or not, and re-evaluated on every run.**
That's what makes price drops work without any special-casing: a unit first
seen at $3200 fails the rent test and sits in the table; when it re-lists at
$2950 it passes, has no `notified_at`, and goes out in that night's digest.

**Source health is tracked explicitly.** A parser that breaks silently produces
zero listings, which is indistinguishable from a quiet market — and the whole
point of this tool is that silence means "nothing matched". `consecutive_empty`
is what lets the run tell those two apart.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    price           INTEGER,
    effective_price INTEGER,
    bedrooms        INTEGER,
    laundry         TEXT,
    parking         TEXT,
    parking_fee     INTEGER,
    lat             REAL,
    lon             REAL,
    neighborhood    TEXT,
    matched         INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    notified_at     TEXT,
    PRIMARY KEY (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_pending
    ON listings (matched, notified_at);

CREATE TABLE IF NOT EXISTS source_health (
    source            TEXT PRIMARY KEY,
    last_run          TEXT,
    last_success      TEXT,
    last_error        TEXT,
    listings_seen     INTEGER NOT NULL DEFAULT 0,
    consecutive_empty INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT PRIMARY KEY,
    finished_at  TEXT,
    matches      INTEGER NOT NULL DEFAULT 0,
    notified     INTEGER NOT NULL DEFAULT 0,
    health_alert INTEGER NOT NULL DEFAULT 0
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SourceHealth:
    source: str
    last_success: str | None
    consecutive_empty: int
    last_error: str | None

    @property
    def is_stale(self) -> bool:
        return self.consecutive_empty > 0


class Store:
    def __init__(self, path: str, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- listings ---------------------------------------------------------

    def seen_ids(self, source: str) -> set[str]:
        """Every external_id we already have for a source.

        Used to skip detail-page fetches for listings we've already parsed —
        the single biggest lever on how much traffic a run generates.
        """
        rows = self.db.execute(
            "SELECT external_id FROM listings WHERE source = ?", (source,)
        ).fetchall()
        return {r["external_id"] for r in rows}

    def hydrate(self, listing) -> None:
        """Fill blanks on a re-observed listing from what we already know.

        Sources only pay for a detail page once — after that a listing comes
        back with just what the *search* page shows: price, title, maybe a
        neighbourhood string. Evaluating that sparse view would reject it for
        "laundry: unknown" and then overwrite the good row with the empty one,
        so a listing that matched on Monday silently stops matching on Tuesday
        and can never recover, because it's in `seen` and never re-fetched.

        Merging the stored detail back in before evaluation is what makes
        re-evaluation safe — and it's re-evaluation that gets price drops for
        free, so this is load-bearing for that too. The fresh price always
        wins; only genuinely-missing fields are backfilled.
        """
        row = self.db.execute(
            "SELECT * FROM listings WHERE source = ? AND external_id = ?",
            (listing.source, listing.external_id),
        ).fetchone()
        if row is None:
            return
        for field in ("bedrooms", "parking_fee", "lat", "lon"):
            if getattr(listing, field, None) is None and row[field] is not None:
                setattr(listing, field, row[field])
        for field in ("laundry", "parking"):
            if getattr(listing, field, "unknown") == "unknown" and row[field]:
                setattr(listing, field, row[field])
        if not listing.body and row["title"]:
            # Keep the scam/keyword text around; it lives only on the detail page.
            listing.body = row["title"]
        if listing.price is None and row["price"] is not None:
            listing.price = row["price"]

    def upsert(self, listing, matched: bool, reject_reason: str | None) -> None:
        """Insert or refresh a listing, preserving first_seen and notified_at.

        notified_at is deliberately never cleared here. If a listing stops
        matching and later matches again, it does not re-notify — you already
        got that link once.
        """
        if self.read_only:
            return
        now = utcnow()
        self.db.execute(
            """
            INSERT INTO listings (
                source, external_id, url, title, price, effective_price, bedrooms,
                laundry, parking, parking_fee, lat, lon, neighborhood,
                matched, reject_reason, first_seen, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                url             = excluded.url,
                title           = excluded.title,
                price           = excluded.price,
                effective_price = excluded.effective_price,
                bedrooms        = excluded.bedrooms,
                laundry         = excluded.laundry,
                parking         = excluded.parking,
                parking_fee     = excluded.parking_fee,
                lat             = excluded.lat,
                lon             = excluded.lon,
                neighborhood    = excluded.neighborhood,
                matched         = excluded.matched,
                reject_reason   = excluded.reject_reason,
                last_seen       = excluded.last_seen
            """,
            (
                listing.source, listing.external_id, listing.url, listing.title,
                listing.price, listing.effective_price, listing.bedrooms,
                listing.laundry, listing.parking, listing.parking_fee,
                listing.lat, listing.lon, listing.neighborhood,
                1 if matched else 0, reject_reason, now, now,
            ),
        )

    def pending_notifications(self) -> list[sqlite3.Row]:
        """Matches we haven't texted about yet, cheapest first."""
        return self.db.execute(
            """
            SELECT * FROM listings
            WHERE matched = 1 AND notified_at IS NULL
            ORDER BY effective_price ASC, first_seen ASC
            """
        ).fetchall()

    def mark_notified(self, rows) -> None:
        if self.read_only:
            return
        now = utcnow()
        self.db.executemany(
            "UPDATE listings SET notified_at = ? WHERE source = ? AND external_id = ?",
            [(now, r["source"], r["external_id"]) for r in rows],
        )

    # -- source health ----------------------------------------------------

    def record_source(self, source: str, count: int, error: str | None) -> SourceHealth:
        now = utcnow()
        row = self.db.execute(
            "SELECT * FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        prev_empty = row["consecutive_empty"] if row else 0
        prev_success = row["last_success"] if row else None

        if count > 0:
            consecutive_empty, last_success = 0, now
        else:
            consecutive_empty, last_success = prev_empty + 1, prev_success

        if not self.read_only:
            self.db.execute(
                """
                INSERT INTO source_health (
                    source, last_run, last_success, last_error, listings_seen,
                    consecutive_empty
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    last_run          = excluded.last_run,
                    last_success      = excluded.last_success,
                    last_error        = excluded.last_error,
                    listings_seen     = excluded.listings_seen,
                    consecutive_empty = excluded.consecutive_empty
                """,
                (source, now, last_success, error, count, consecutive_empty),
            )
        return SourceHealth(source, last_success, consecutive_empty, error)

    # -- runs -------------------------------------------------------------

    def last_health_alert(self) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(started_at) AS t FROM runs WHERE health_alert = 1"
        ).fetchone()
        if not row or not row["t"]:
            return None
        try:
            return datetime.fromisoformat(row["t"])
        except ValueError:
            return None

    def health_alert_allowed(self, cooldown_days: int) -> bool:
        """Rate-limit the "a source looks broken" text.

        Without this, a permanently dead parser texts you every single day
        forever, which trains you to ignore the one channel this tool has.
        """
        last = self.last_health_alert()
        if last is None:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(days=cooldown_days)

    def start_run(self) -> str:
        started = utcnow()
        if not self.read_only:
            self.db.execute(
                "INSERT OR REPLACE INTO runs (started_at) VALUES (?)", (started,)
            )
            self.db.commit()
        return started

    def finish_run(self, started: str, matches: int, notified: int, health_alert: bool) -> None:
        if self.read_only:
            return
        self.db.execute(
            """
            UPDATE runs SET finished_at = ?, matches = ?, notified = ?, health_alert = ?
            WHERE started_at = ?
            """,
            (utcnow(), matches, notified, 1 if health_alert else 0, started),
        )
        self.db.commit()

    def commit(self) -> None:
        if not self.read_only:
            self.db.commit()


def today_key() -> str:
    """Date stamp used for the SMS idempotency key."""
    return date.today().isoformat()
