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
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Postings run long and we only ever pattern-match them. This is well past the
# point where a rule would fire and keeps the SQLite file on the PVC small.
BODY_LIMIT = 4000

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
    -- The posting text. Stored, not just parsed: every later run re-evaluates
    -- from the database (see hydrate), and the rules that read this — room
    -- share, the whole scam filter, exclude_keywords — are the ones that must
    -- not change their verdict between run one and run two.
    body            TEXT,
    note            TEXT,
    image_url       TEXT,
    notified_run    TEXT,
    rent_controlled INTEGER,
    year_built      INTEGER,
    distance_mi     REAL,
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

-- Assessor lookups, keyed by address or rounded lat/lon. Buildings do not
-- change their year of construction, so these are cached forever — including
-- misses, so a parcel with no record isn't re-queried every run.
CREATE TABLE IF NOT EXISTS building_info (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_rent (
    beds       INTEGER PRIMARY KEY,
    amount     INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT PRIMARY KEY,
    token        TEXT,
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


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a
# no-op against an existing table, so a new column in SCHEMA reaches a fresh
# database and silently misses every deployed one — which surfaces as
# "table listings has no column named X" on the next run, for every source at
# once. Anything added to `listings` from now on belongs here too.
MIGRATIONS = [
    ("listings", "note", "TEXT"),
    ("listings", "image_url", "TEXT"),
    ("listings", "notified_run", "TEXT"),
    ("listings", "rent_controlled", "INTEGER"),
    ("listings", "year_built", "INTEGER"),
    ("listings", "distance_mi", "REAL"),
    ("listings", "body", "TEXT"),
    ("runs", "token", "TEXT"),
]


class Store:
    def __init__(self, path: str, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # WAL: the web process reads this file while a run writes it.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add any columns missing from an older database."""
        for table, column, coltype in MIGRATIONS:
            existing = {
                r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")
            }
            if not existing or column in existing:
                continue
            logger.info("migrating: adding %s.%s", table, column)
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- listings ---------------------------------------------------------

    def seen_ids(self, source: str) -> set[str]:
        """Every external_id we already have *and have read the body of*.

        Used to skip detail-page fetches for listings we've already parsed —
        the single biggest lever on how much traffic a run generates.

        The body condition is what makes this honest. A row with no stored body
        was never really parsed (or was stored before there was a column to put
        it in), and re-evaluating it forever against its title alone is exactly
        the blindness that texted a room share. Letting those rows cost one
        more detail fetch, once, is cheap and self-limiting.
        """
        rows = self.db.execute(
            "SELECT external_id FROM listings "
            "WHERE source = ? AND body IS NOT NULL AND body != ''",
            (source,),
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
        if not listing.body:
            # The posting text, which lives only on the detail page and is only
            # fetched once. Falling back to the *title* here (as this did) is
            # how a room share got texted: it was correctly rejected on the run
            # that read its body — "Room available in a bright 2-bed" — and
            # then passed an hour later, when the only text left to judge was
            # "Center North Beach w/stunning views". Every body rule flipped,
            # silently, including the entire scam filter.
            listing.body = row["body"] or row["title"] or ""
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
                laundry, parking, parking_fee, lat, lon, neighborhood, body, note, image_url,
                rent_controlled, year_built, distance_mi,
                matched, reject_reason, first_seen, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                -- COALESCE, so a re-observation that arrives without a detail
                -- page can't erase the text we already paid a fetch for.
                body            = COALESCE(excluded.body, listings.body),
                note            = excluded.note,
                image_url       = COALESCE(excluded.image_url, listings.image_url),
                rent_controlled = excluded.rent_controlled,
                year_built      = excluded.year_built,
                distance_mi     = excluded.distance_mi,
                matched         = excluded.matched,
                reject_reason   = excluded.reject_reason,
                last_seen       = excluded.last_seen
            """,
            (
                listing.source, listing.external_id, listing.url, listing.title,
                listing.price, listing.effective_price, listing.bedrooms,
                listing.laundry, listing.parking, listing.parking_fee,
                listing.lat, listing.lon, listing.neighborhood,
                (getattr(listing, "body", "") or "")[:BODY_LIMIT] or None,
                getattr(listing, "note", ""),
                getattr(listing, "image_url", None),
                getattr(listing, "rent_controlled", None),
                getattr(listing, "year_built", None),
                getattr(listing, "distance_mi", None),
                1 if matched else 0, reject_reason, now, now,
            ),
        )

    def mark_dead(self, source: str, external_id: str, reason: str) -> None:
        """Retire a listing that no longer exists.

        matched=0 keeps it out of `pending_notifications`, and notified_at is
        stamped so it can't come back if the URL starts answering again — a
        removed post that reappears is a repost, and reposts arrive with their
        own id anyway.
        """
        if self.read_only:
            return
        self.db.execute(
            "UPDATE listings SET matched = 0, reject_reason = ?, notified_at = COALESCE(notified_at, ?) "
            "WHERE source = ? AND external_id = ?",
            (reason, utcnow(), source, external_id),
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

    def recently_notified(self, days: int = 7) -> list[sqlite3.Row]:
        """Rows already texted about in the last `days`, for a liveness sweep.

        Run pages are permanent links, so a post removed after its page was
        minted would keep sending you to a dead listing. Re-checking the recent
        ones each send lets the page mark them instead.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.db.execute(
            "SELECT * FROM listings WHERE notified_at IS NOT NULL AND notified_at >= ? "
            "AND (reject_reason IS NULL OR reject_reason NOT LIKE 'removed%')",
            (cutoff,),
        ).fetchall()

    def mark_notified(self, rows, run_token: str | None = None) -> None:
        """Stamp the send, and which run page these listings appear on.

        The token is what the SMS links to: a page shows exactly the listings
        from one run, so the text can be a single short link instead of a wall
        of URLs.
        """
        if self.read_only:
            return
        now = utcnow()
        self.db.executemany(
            "UPDATE listings SET notified_at = ?, notified_run = ? WHERE source = ? AND external_id = ?",
            [(now, run_token, r["source"], r["external_id"]) for r in rows],
        )

    def listings_for_run(self, token: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM listings WHERE notified_run = ? ORDER BY effective_price ASC, first_seen ASC",
            (token,),
        ).fetchall()

    def run_meta(self, token: str):
        return self.db.execute(
            "SELECT * FROM runs WHERE token = ?", (token,)
        ).fetchone()

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

    # -- building info ----------------------------------------------------

    def building_info(self, key: str):
        """Cached assessor answer. {} means "looked up, found nothing"; None
        means "never looked up"."""
        row = self.db.execute(
            "SELECT payload FROM building_info WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            return None

    def save_building_info(self, key: str, info: dict) -> None:
        if self.read_only:
            return
        self.db.execute(
            """INSERT INTO building_info (key, payload, fetched_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   payload = excluded.payload, fetched_at = excluded.fetched_at""",
            (key, json.dumps(info), utcnow()),
        )
        self.commit()

    # -- market rent ------------------------------------------------------

    def market_rent(self) -> tuple[dict[int, int] | None, int | None]:
        """(rents, age_in_days) for the cached market figures, or (None, None)."""
        rows = self.db.execute("SELECT beds, amount, fetched_at FROM market_rent").fetchall()
        if not rows:
            return None, None
        rents = {int(r["beds"]): int(r["amount"]) for r in rows}
        newest = max(r["fetched_at"] for r in rows)
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(newest)).days
        except ValueError:
            age = None
        return rents, age

    def save_market_rent(self, rents: dict[int, int]) -> None:
        if self.read_only:
            return
        now = utcnow()
        self.db.executemany(
            """INSERT INTO market_rent (beds, amount, fetched_at) VALUES (?,?,?)
               ON CONFLICT(beds) DO UPDATE SET
                   amount = excluded.amount, fetched_at = excluded.fetched_at""",
            [(b, a, now) for b, a in rents.items()],
        )
        self.commit()

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

    def set_run_token(self, started: str, token: str) -> None:
        if self.read_only:
            return
        self.db.execute("UPDATE runs SET token = ? WHERE started_at = ?", (token, started))
        self.commit()

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
