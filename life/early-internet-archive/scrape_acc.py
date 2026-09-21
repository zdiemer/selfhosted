#!/usr/bin/env python3
"""Recover posts by the exact `Irock` byline from archived ACC threads."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import html
import json
import random
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CDX_URL = "https://web.archive.org/cdx/search/cdx"
THREAD_PREFIX = "www.animalcrossingcommunity.com/thread_messages.asp"
USERNAME = "Irock"
USER_AGENT = "ZachDiemerPersonalArchive/1.0"
START_TIMESTAMP = "20040101000000"
END_TIMESTAMP = "20061231235959"
PATTERN_LISTING_CAPTURES = {
    1: "20220505204342",
    2: "20220505204340",
    3: "20220505204343",
    4: "20220505221500",
    5: "20220505233940",
    6: "20220506010239",
    7: "20220506022140",
    8: "20220506033444",
    9: "20220506044134",
    10: "20220506054508",
    11: "20220506064549",
    12: "20220506074103",
    13: "20220506083258",
    14: "20220506092252",
    15: "20220506101755",
    16: "20220506110330",
    17: "20220506115618",
    18: "20220506125330",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_bytes(url: str, timeout: int = 90, retries: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            if b"Internet Archive: Temporarily Offline" in payload:
                raise OSError("Internet Archive temporarily offline")
            return payload
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise
            if attempt + 1 == retries:
                raise
        except (OSError, urllib.error.URLError):
            if attempt + 1 == retries:
                raise
        time.sleep(min(10, 2**attempt) + random.random())
    raise AssertionError("unreachable")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identities (
            username TEXT PRIMARY KEY,
            user_id TEXT,
            confidence TEXT NOT NULL,
            evidence TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS page_attempts (
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            thread_id TEXT,
            page_number INTEGER,
            digest TEXT,
            cdx_length INTEGER,
            result TEXT NOT NULL,
            raw_path TEXT,
            raw_sha256 TEXT,
            parsed_post_count INTEGER NOT NULL DEFAULT 0,
            matched_post_count INTEGER NOT NULL DEFAULT 0,
            fetch_error TEXT,
            attempted_at TEXT NOT NULL,
            PRIMARY KEY(timestamp, original_url)
        );
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            thread_title TEXT NOT NULL,
            content_html TEXT NOT NULL,
            content_text TEXT NOT NULL,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            posted_at TEXT,
            canonical_url TEXT NOT NULL,
            capture_timestamp TEXT NOT NULL,
            snapshot_url TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            recovered_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS acc_posts_date_idx ON posts(posted_at);
        CREATE INDEX IF NOT EXISTS acc_posts_thread_idx ON posts(thread_id);
        CREATE TABLE IF NOT EXISTS patterns (
            pattern_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author_name TEXT NOT NULL,
            published_at TEXT NOT NULL,
            votes INTEGER,
            score REAL,
            detail_url TEXT NOT NULL,
            image_url TEXT NOT NULL,
            listing_capture_timestamp TEXT NOT NULL,
            listing_raw_path TEXT NOT NULL,
            recovered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pattern_observations (
            pattern_id TEXT NOT NULL REFERENCES patterns(pattern_id) ON DELETE CASCADE,
            capture_timestamp TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TEXT NOT NULL,
            votes INTEGER,
            score REAL,
            listing_raw_path TEXT NOT NULL,
            PRIMARY KEY(pattern_id, capture_timestamp)
        );
        CREATE TABLE IF NOT EXISTS pattern_assets (
            pattern_id TEXT PRIMARY KEY REFERENCES patterns(pattern_id) ON DELETE CASCADE,
            original_url TEXT NOT NULL,
            snapshot_url TEXT NOT NULL,
            capture_timestamp TEXT,
            mime_type TEXT NOT NULL,
            media_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            recovered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_posts (
            requested_post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
            context_post_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            posted_at TEXT,
            content_html TEXT NOT NULL,
            content_text TEXT NOT NULL,
            PRIMARY KEY(requested_post_id, context_post_id)
        );
        """
    )
    db.execute(
        """INSERT INTO identities(username,user_id,confidence,evidence,resolved_at)
           VALUES(?,NULL,'confirmed',?,NULL)
           ON CONFLICT(username) DO NOTHING""",
        (
            USERNAME,
            "First-person NSider2 posts identify Irock as the author's 2004 ACC account.",
        ),
    )
    db.commit()
    return db


def set_meta(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        """INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), utc_now()),
    )


def decode_html(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("windows-1252", "replace")


def plain_text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<br\s*/?>|</p\s*>|</li\s*>|</div\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in fragment.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def thread_coordinates(url: str) -> tuple[str, int]:
    decoded = html.unescape(url)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(decoded).query)
    thread_id = (query.get("ThreadID") or [""])[0]
    page = int((query.get("PageNumber") or ["1"])[0] or 1)
    if not thread_id:
        match = re.search(r"/ThreadID/(\d+)", decoded, re.I)
        thread_id = match.group(1) if match else ""
    page_match = re.search(r"/PageNumber/(\d+)", decoded, re.I)
    if page_match:
        page = int(page_match.group(1))
    return thread_id, page


def parse_acc_datetime(value: str, capture_timestamp: str) -> str | None:
    value = re.sub(r"\s+", " ", plain_text(value)).strip()
    if not value:
        return None
    for fmt in ("%m/%d/%Y %I:%M%p", "%m/%d/%y %I:%M%p"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}:\d{2}(?:am|pm))", value, re.I)
    if match:
        captured = datetime.strptime(capture_timestamp[:8], "%Y%m%d")
        month = int(match.group(1))
        year = captured.year - 1 if month > captured.month + 2 else captured.year
        try:
            return datetime.strptime(
                f"{month}/{match.group(2)}/{year} {match.group(3)}", "%m/%d/%Y %I:%M%p"
            ).isoformat()
        except ValueError:
            pass
    if re.fullmatch(r"\d{1,2}:\d{2}(?:am|pm)", value, re.I):
        try:
            captured = datetime.strptime(capture_timestamp[:8], "%Y%m%d")
            clock = datetime.strptime(value, "%I:%M%p")
            return captured.replace(hour=clock.hour, minute=clock.minute).isoformat()
        except ValueError:
            pass
    return value


AUTHOR_RE = re.compile(
    r"<td\b[^>]*class\s*=\s*['\"]?threadbold['\"]?[^>]*>.*?"
    r"<a\b[^>]*href\s*=\s*['\"]?user_profile\.asp\?UserID=(\d+)[^>]*>"
    r"(.*?)</a>",
    re.I | re.S,
)


def parse_page(source: str, original_url: str, capture_timestamp: str) -> dict[str, Any]:
    thread_id, page_number = thread_coordinates(original_url)
    title_match = re.search(r"<title>\s*Animal Crossing Community:\s*(.*?)</title>", source, re.I | re.S)
    if not title_match:
        title_match = re.search(r"\bTopic:\s*(.*?)</td>", source, re.I | re.S)
    title = plain_text(title_match.group(1)) if title_match else f"ACC thread {thread_id}"
    authors = list(AUTHOR_RE.finditer(source))
    posts: list[dict[str, Any]] = []
    for sequence, author in enumerate(authors):
        end = authors[sequence + 1].start() if sequence + 1 < len(authors) else len(source)
        chunk = source[author.start():end]
        message = re.search(
            r"<div\b[^>]*class\s*=\s*['\"]?threadmessage['\"]?[^>]*>(.*?)"
            r"</div>\s*(?:<table\b[^>]*>\s*<tr>\s*<td\b[^>]*class\s*=\s*['\"]?signature|</td>)",
            chunk,
            re.I | re.S,
        )
        if not message:
            continue
        posted = re.search(
            r"<div\b[^>]*class\s*=\s*['\"]?postdate['\"]?[^>]*>.*?"
            r"<b>\s*Posted:\s*</b>(.*?)</div>",
            chunk,
            re.I | re.S,
        )
        author_id = author.group(1)
        author_name = plain_text(author.group(2))
        body_html = message.group(1).strip()
        posted_at = parse_acc_datetime(posted.group(1), capture_timestamp) if posted else None
        fingerprint = "\0".join(
            [thread_id, str(page_number), str(sequence), author_id, posted_at or "", plain_text(body_html)]
        )
        post_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        posts.append(
            {
                "post_id": post_id,
                "thread_id": thread_id,
                "page_number": page_number,
                "sequence": sequence,
                "thread_title": title,
                "content_html": body_html,
                "content_text": plain_text(body_html),
                "author_id": author_id,
                "author_name": author_name,
                "posted_at": posted_at,
            }
        )
    return {
        "thread_id": thread_id,
        "page_number": page_number,
        "thread_title": title,
        "posts": posts,
    }


PATTERN_ROW_RE = re.compile(
    r"pattern_view\.asp\?PatternID=(\d+)[^>]*>.*?</a></td>\s*"
    r"<td>&nbsp;</td><td[^>]*><a[^>]*>(.*?)</a>.*?</tr></table></td>\s*"
    r"<td[^>]*><a[^>]*>(.*?)</a></td>\s*"
    r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>",
    re.I | re.S,
)


def parse_patterns_page(source: str) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    for match in PATTERN_ROW_RE.finditer(source):
        author = plain_text(match.group(3))
        if author.casefold() != USERNAME.casefold():
            continue
        pattern_id = match.group(1)
        title = plain_text(match.group(2)).rstrip("\\").strip()
        published = plain_text(match.group(4))
        votes_text = re.sub(r"\D", "", plain_text(match.group(5)))
        score_text = plain_text(match.group(6))
        patterns.append(
            {
                "pattern_id": pattern_id,
                "title": title,
                "author_name": USERNAME,
                "published_at": normalize_acc_date(published),
                "votes": int(votes_text) if votes_text else None,
                "score": float(score_text) if re.fullmatch(r"\d+(?:\.\d+)?", score_text) else None,
                "detail_url": (
                    "http://www.animalcrossingcommunity.com/"
                    f"pattern_view.asp?PatternID={pattern_id}"
                ),
                "image_url": (
                    "http://www.animalcrossingcommunity.com/"
                    f"pattern_image.asp?PatternID={pattern_id}"
                ),
            }
        )
    return patterns


def normalize_acc_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value


def preserve_pattern_pages(
    db: sqlite3.Connection,
    root: Path,
    seed_directory: Path,
) -> int:
    recovered = 0
    for seed in sorted(seed_directory.glob("page-*.html")):
        payload = seed.read_bytes()
        source = decode_html(payload)
        parsed = parse_patterns_page(source)
        if not parsed:
            continue
        page_match = re.search(r"page-(\d+)", seed.name, re.I)
        page_number = int(page_match.group(1)) if page_match else 1
        capture_timestamp = "20061017203848" if page_number == 1 else "20220505190449"
        recovered += store_pattern_page(
            db, root, page_number, capture_timestamp, payload, parsed
        )
    db.commit()
    return recovered


def store_pattern_page(
    db: sqlite3.Connection,
    root: Path,
    page_number: int,
    capture_timestamp: str,
    payload: bytes,
    parsed: list[dict[str, Any]] | None = None,
) -> int:
    patterns = parsed if parsed is not None else parse_patterns_page(decode_html(payload))
    if not patterns:
        raise ValueError(f"pattern listing page {page_number} yielded no exact Irock rows")
    raw = root / "raw" / "pattern-listings" / f"{capture_timestamp}--page-{page_number}.html.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        with gzip.open(raw, "wb", compresslevel=9) as output:
            output.write(payload)
    relative = str(raw.relative_to(root))
    for pattern in patterns:
        db.execute(
            """INSERT INTO patterns(
                   pattern_id,title,author_name,published_at,votes,score,detail_url,
                   image_url,listing_capture_timestamp,listing_raw_path,recovered_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pattern_id) DO UPDATE SET
                   title=excluded.title,author_name=excluded.author_name,
                   published_at=excluded.published_at,votes=excluded.votes,
                   score=excluded.score,detail_url=excluded.detail_url,
                   image_url=excluded.image_url,
                   listing_capture_timestamp=excluded.listing_capture_timestamp,
                   listing_raw_path=excluded.listing_raw_path,
                   recovered_at=excluded.recovered_at""",
            (
                pattern["pattern_id"], pattern["title"], pattern["author_name"],
                pattern["published_at"], pattern["votes"], pattern["score"],
                pattern["detail_url"], pattern["image_url"], capture_timestamp,
                relative, utc_now(),
            ),
        )
        db.execute(
            """INSERT OR REPLACE INTO pattern_observations(
                   pattern_id,capture_timestamp,title,published_at,votes,score,
                   listing_raw_path
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                pattern["pattern_id"], capture_timestamp, pattern["title"],
                pattern["published_at"], pattern["votes"], pattern["score"], relative,
            ),
        )
    db.commit()
    return len(patterns)


def recover_pattern_listing_captures(
    db: sqlite3.Connection, root: Path, delay: float
) -> tuple[int, int]:
    recovered = errors = 0
    for index, (page, timestamp) in enumerate(sorted(PATTERN_LISTING_CAPTURES.items()), 1):
        exists = db.execute(
            "SELECT 1 FROM pattern_observations WHERE capture_timestamp=? LIMIT 1",
            (timestamp,),
        ).fetchone()
        if exists:
            continue
        original = (
            "http://www.animalcrossingcommunity.com/patterns.asp?"
            f"UserLogin=Irock&PatternName=&ListAll=True&PageNumber={page}&SortBy=5"
        )
        snapshot = f"https://web.archive.org/web/{timestamp}id_/{original}"
        try:
            payload = request_bytes(snapshot, retries=4)
            count = store_pattern_page(db, root, page, timestamp, payload)
            recovered += count
            print(f"[pattern page {index}/{len(PATTERN_LISTING_CAPTURES)}] page {page}: {count}", flush=True)
        except Exception as error:
            errors += 1
            print(
                f"[pattern page {index}/{len(PATTERN_LISTING_CAPTURES)}] page {page}: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
        if index < len(PATTERN_LISTING_CAPTURES) and delay:
            time.sleep(delay)
    return recovered, errors


def gif_payload(payload: bytes) -> bool:
    return payload.startswith((b"GIF87a", b"GIF89a"))


def preserve_pattern_images(db: sqlite3.Connection, root: Path, delay: float) -> tuple[int, int]:
    rows = db.execute(
        """SELECT p.pattern_id,p.image_url FROM patterns p
           LEFT JOIN pattern_assets a USING(pattern_id)
           WHERE a.pattern_id IS NULL ORDER BY CAST(p.pattern_id AS INTEGER)"""
    ).fetchall()
    saved = errors = 0
    for index, row in enumerate(rows, 1):
        snapshot = f"https://web.archive.org/web/20061017203848id_/{row['image_url']}"
        try:
            payload = request_bytes(snapshot, retries=4)
            if not gif_payload(payload):
                raise ValueError("replay did not return a GIF payload")
            media = root / "media" / "patterns" / f"{row['pattern_id']}.gif"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            db.execute(
                """INSERT OR REPLACE INTO pattern_assets(
                       pattern_id,original_url,snapshot_url,capture_timestamp,mime_type,
                       media_path,sha256,byte_length,recovered_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    row["pattern_id"], row["image_url"], snapshot, "20220505",
                    "image/gif", str(media.relative_to(root)), digest, len(payload), utc_now(),
                ),
            )
            db.commit()
            saved += 1
            print(f"[pattern image {index}/{len(rows)}] saved {row['pattern_id']}", flush=True)
        except Exception as error:
            errors += 1
            print(
                f"[pattern image {index}/{len(rows)}] {row['pattern_id']}: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
        if index < len(rows) and delay:
            time.sleep(delay)
    return saved, errors


def cdx_query() -> bytes:
    params: list[tuple[str, str]] = [
        ("url", THREAD_PREFIX),
        ("matchType", "prefix"),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode,digest,length"),
        ("filter", "statuscode:200"),
        ("from", "2004"),
        ("to", "2006"),
        ("collapse", "urlkey"),
    ]
    return request_bytes(f"{CDX_URL}?{urllib.parse.urlencode(params)}", timeout=300)


def load_cdx(cache: Path, seed: Path | None = None) -> list[dict[str, str]]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        if seed:
            shutil.copyfile(seed, cache)
        else:
            cache.write_bytes(cdx_query())
    rows = json.loads(cache.read_bytes())
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:] if row and row[0]]


def candidates(
    db: sqlite3.Connection, rows: Iterable[dict[str, str]], retry_failures: bool
) -> list[dict[str, str]]:
    attempted = {
        (row["timestamp"], row["original_url"]): row["result"]
        for row in db.execute("SELECT timestamp,original_url,result FROM page_attempts")
    }
    selected = []
    for row in rows:
        if not START_TIMESTAMP <= row["timestamp"] <= END_TIMESTAMP:
            continue
        thread_id, _ = thread_coordinates(row["original"])
        if not thread_id:
            continue
        previous = attempted.get((row["timestamp"], row["original"]))
        if previous and not (retry_failures and previous == "fetch_error"):
            continue
        selected.append(row)
    return sorted(selected, key=lambda row: (row["timestamp"], row["original"]))


def store_success(
    db: sqlite3.Connection,
    root: Path,
    row: dict[str, str],
    payload: bytes,
) -> tuple[int, int, str | None]:
    parsed = parse_page(decode_html(payload), row["original"], row["timestamp"])
    matches = [post for post in parsed["posts"] if post["author_name"].casefold() == USERNAME.casefold()]
    raw_path: Path | None = None
    raw_sha256 = hashlib.sha256(payload).hexdigest()
    if matches:
        key = hashlib.sha256(f"{row['timestamp']}\0{row['original']}".encode()).hexdigest()[:20]
        raw_path = root / "raw" / "matched-pages" / f"{row['timestamp']}--{key}.html.gz"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            with gzip.open(raw_path, "wb", compresslevel=9) as output:
                output.write(payload)
    relative = str(raw_path.relative_to(root)) if raw_path else None
    snapshot = f"https://web.archive.org/web/{row['timestamp']}/{row['original']}"
    for post in matches:
        canonical = html.unescape(row["original"])
        db.execute(
            """INSERT INTO posts(
                   post_id,thread_id,page_number,sequence,thread_title,content_html,
                   content_text,author_id,author_name,posted_at,canonical_url,
                   capture_timestamp,snapshot_url,raw_path,raw_sha256,recovered_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(post_id) DO UPDATE SET
                   thread_title=excluded.thread_title,content_html=excluded.content_html,
                   content_text=excluded.content_text,author_name=excluded.author_name,
                   posted_at=excluded.posted_at,capture_timestamp=excluded.capture_timestamp,
                   snapshot_url=excluded.snapshot_url,raw_path=excluded.raw_path,
                   raw_sha256=excluded.raw_sha256,recovered_at=excluded.recovered_at""",
            (
                post["post_id"], post["thread_id"], post["page_number"], post["sequence"],
                post["thread_title"], post["content_html"], post["content_text"],
                post["author_id"], post["author_name"], post["posted_at"], canonical,
                row["timestamp"], snapshot, relative, raw_sha256, utc_now(),
            ),
        )
        db.execute("DELETE FROM context_posts WHERE requested_post_id=?", (post["post_id"],))
        for context in parsed["posts"]:
            db.execute(
                """INSERT INTO context_posts(
                       requested_post_id,context_post_id,sequence,author_id,author_name,
                       posted_at,content_html,content_text
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    post["post_id"], context["post_id"], context["sequence"],
                    context["author_id"], context["author_name"], context["posted_at"],
                    context["content_html"], context["content_text"],
                ),
            )
    user_ids = sorted({post["author_id"] for post in matches})
    resolved_id = user_ids[0] if len(user_ids) == 1 else None
    if resolved_id:
        db.execute(
            """UPDATE identities SET user_id=?,resolved_at=? WHERE username=?
               AND (user_id IS NULL OR user_id=?)""",
            (resolved_id, utc_now(), USERNAME, resolved_id),
        )
    db.execute(
        """INSERT OR REPLACE INTO page_attempts(
               timestamp,original_url,thread_id,page_number,digest,cdx_length,result,
               raw_path,raw_sha256,parsed_post_count,matched_post_count,fetch_error,attempted_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
        (
            row["timestamp"], row["original"], parsed["thread_id"], parsed["page_number"],
            row.get("digest"), int(row.get("length") or 0),
            "matched" if matches else "no_match", relative, raw_sha256,
            len(parsed["posts"]), len(matches), utc_now(),
        ),
    )
    db.commit()
    return len(parsed["posts"]), len(matches), resolved_id


def store_error(db: sqlite3.Connection, row: dict[str, str], error: Exception) -> None:
    thread_id, page = thread_coordinates(row["original"])
    db.execute(
        """INSERT OR REPLACE INTO page_attempts(
               timestamp,original_url,thread_id,page_number,digest,cdx_length,result,
               parsed_post_count,matched_post_count,fetch_error,attempted_at
           ) VALUES(?,?,?,?,?,?,'fetch_error',0,0,?,?)""",
        (
            row["timestamp"], row["original"], thread_id, page, row.get("digest"),
            int(row.get("length") or 0), f"{type(error).__name__}: {error}", utc_now(),
        ),
    )
    db.commit()


def fetch(row: dict[str, str]) -> tuple[dict[str, str], bytes | None, Exception | None]:
    snapshot = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
    try:
        return row, request_bytes(snapshot), None
    except Exception as error:
        return row, None, error


def status(db: sqlite3.Connection) -> dict[str, Any]:
    identity = db.execute("SELECT * FROM identities WHERE username=?", (USERNAME,)).fetchone()
    attempts = dict(db.execute("SELECT result,COUNT(*) FROM page_attempts GROUP BY result"))
    dates = db.execute("SELECT MIN(posted_at),MAX(posted_at) FROM posts").fetchone()
    return {
        "identity": dict(identity) if identity else None,
        "attempts": attempts,
        "posts": db.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
        "threads": db.execute("SELECT COUNT(DISTINCT thread_id) FROM posts").fetchone()[0],
        "context_rows": db.execute("SELECT COUNT(*) FROM context_posts").fetchone()[0],
        "patterns": db.execute("SELECT COUNT(*) FROM patterns").fetchone()[0],
        "pattern_observations": db.execute(
            "SELECT COUNT(*) FROM pattern_observations"
        ).fetchone()[0],
        "pattern_assets": db.execute("SELECT COUNT(*) FROM pattern_assets").fetchone()[0],
        "first_post": dates[0],
        "last_post": dates[1],
    }


def export_jsonl(db: sqlite3.Connection, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for row in db.execute("SELECT * FROM posts ORDER BY posted_at,post_id"):
            output.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent / "data" / "acc")
    parser.add_argument("--seed-thread-cdx", type=Path)
    parser.add_argument("--limit", type=int, default=250, help="maximum new page attempts; 0 is unlimited")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--seed-pattern-pages", type=Path)
    parser.add_argument("--download-pattern-images", action="store_true")
    parser.add_argument("--recover-pattern-pages", action="store_true")
    parser.add_argument("--pattern-page-delay", type=float, default=8.0)
    parser.add_argument("--pattern-image-delay", type=float, default=2.5)
    parser.add_argument("--patterns-only", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    db = connect(args.root / "acc.sqlite3")
    try:
        if args.status:
            print(json.dumps(status(db), indent=2, ensure_ascii=False))
            return
        if args.seed_pattern_pages:
            count = preserve_pattern_pages(db, args.root, args.seed_pattern_pages)
            print(f"recovered {count} pattern rows from seeded listing pages", flush=True)
        if args.recover_pattern_pages:
            recovered, errors = recover_pattern_listing_captures(
                db, args.root, args.pattern_page_delay
            )
            print(f"pattern pages: {recovered} rows, {errors} errors", flush=True)
        if args.download_pattern_images:
            saved, errors = preserve_pattern_images(db, args.root, args.pattern_image_delay)
            print(f"pattern images: {saved} saved, {errors} errors", flush=True)
        if args.patterns_only:
            set_meta(db, "last_run", status(db))
            db.commit()
            print(json.dumps(status(db), indent=2, ensure_ascii=False))
            return
        rows = load_cdx(args.root / "raw" / "cdx-thread-messages.json", args.seed_thread_cdx)
        set_meta(db, "cdx_inventory_count", len(rows))
        set_meta(db, "scan_window", {"from": START_TIMESTAMP, "to": END_TIMESTAMP})
        work = candidates(db, rows, args.retry_failures)
        if args.limit:
            work = work[: args.limit]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for index, (row, payload, error) in enumerate(pool.map(fetch, work), 1):
                if error or payload is None:
                    store_error(db, row, error or RuntimeError("empty response"))
                    print(f"[{index}/{len(work)}] fetch_error {row['original']}", flush=True)
                    continue
                parsed, matched, user_id = store_success(db, args.root, row, payload)
                if matched or index % 50 == 0 or index == len(work):
                    suffix = f"; resolved UserID={user_id}" if user_id else ""
                    print(
                        f"[{index}/{len(work)}] {parsed} posts, {matched} Irock matches{suffix}",
                        flush=True,
                    )
        set_meta(db, "last_run", status(db))
        db.commit()
        export_jsonl(db, args.root / "posts.jsonl")
        print(json.dumps(status(db), indent=2, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
