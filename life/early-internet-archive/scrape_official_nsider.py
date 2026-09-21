#!/usr/bin/env python3
"""Resumable, evidence-preserving collector for STARFOXA on Official NSider."""

from __future__ import annotations

import argparse
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
FORUM_PREFIX = "forums.nintendo.com/nintendo/board/message"
PROFILE_URL = "http://forums.nintendo.com/nintendo/view_profile?user.id=106819"
PROFILE_TIMESTAMP = "20070822021707"
USER_ID = "106819"
USERNAME = "STARFOXA"
USER_AGENT = "ZachDiemerPersonalArchive/1.0"
DEFAULT_BOARDS = ("np_po",)
REGISTERED_TIMESTAMP = "20050605000000"


class ArchiveUnavailable(RuntimeError):
    """The archive service is globally unavailable, not a capture failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_bytes(url: str, timeout: int = 120, retries: int = 5) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            if b"Internet Archive: Temporarily Offline" in payload:
                raise ArchiveUnavailable("Internet Archive temporarily offline")
            return payload
        except ArchiveUnavailable:
            raise
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise
            if attempt + 1 == retries:
                raise
        except (OSError, urllib.error.URLError):
            if attempt + 1 == retries:
                raise
        time.sleep(min(30, 2**attempt) + random.random())
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
        CREATE TABLE IF NOT EXISTS profiles (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            registered_at TEXT,
            last_visited_at TEXT,
            total_posts INTEGER,
            rank TEXT,
            capture_timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            parsed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS page_attempts (
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            board_id TEXT,
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
            board_id TEXT NOT NULL,
            thread_title TEXT NOT NULL,
            post_title TEXT NOT NULL,
            content_html TEXT NOT NULL,
            content_text TEXT NOT NULL,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            posted_at TEXT,
            reply_number INTEGER,
            reply_count INTEGER,
            canonical_url TEXT NOT NULL,
            capture_timestamp TEXT NOT NULL,
            snapshot_url TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            recovered_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS posts_date_idx ON posts(posted_at);
        CREATE INDEX IF NOT EXISTS posts_thread_idx ON posts(thread_id);
        CREATE TABLE IF NOT EXISTS context_posts (
            requested_post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
            context_post_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            author_id TEXT,
            author_name TEXT,
            posted_at TEXT,
            post_title TEXT,
            content_html TEXT NOT NULL,
            content_text TEXT NOT NULL,
            PRIMARY KEY(requested_post_id, context_post_id)
        );
        CREATE INDEX IF NOT EXISTS context_anchor_idx
            ON context_posts(requested_post_id, sequence);
        """
    )
    return db


def set_meta(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        """INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), utc_now()),
    )


def plain_text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<br\s*/?>|</p\s*>|</li\s*>|</div\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in fragment.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def decode_html(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("windows-1252", "replace")


def parse_nsider_datetime(date: str | None, clock: str | None) -> str | None:
    if not date:
        return None
    value = f"{date.strip()} {(clock or '').strip()}".strip()
    for fmt in ("%m-%d-%Y %I:%M %p", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            pass
    return value


def labeled_value(source: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}.*?<td\b[^>]*>(.*?)</td>", source, re.I | re.S
    )
    return plain_text(match.group(1)) if match else None


def parse_profile(source: str) -> dict[str, Any]:
    title = re.search(r"View Profile for\s+(.*?)\s*-", source, re.I | re.S)
    username = plain_text(title.group(1)) if title else ""
    registered = re.search(
        r"Date Registered.*?<span\s+class=date_text>(.*?)</span>\s*"
        r"<span\s+class=time_text>(.*?)</span>",
        source,
        re.I | re.S,
    )
    visited = re.search(
        r"Date Last Visited.*?<span\s+class=date_text>(.*?)</span>\s*"
        r"<span\s+class=time_text>(.*?)</span>",
        source,
        re.I | re.S,
    )
    total = labeled_value(source, "Total Posts")
    rank = labeled_value(source, "Rank")
    if username.upper() != USERNAME:
        raise ValueError(f"profile identity mismatch: {username!r}")
    return {
        "username": username,
        "registered_at": parse_nsider_datetime(*registered.groups()) if registered else None,
        "last_visited_at": parse_nsider_datetime(*visited.groups()) if visited else None,
        "total_posts": int(re.sub(r"\D", "", total or "0")) or None,
        "rank": rank,
    }


def attribute_value(tag: str, attribute: str) -> str | None:
    match = re.search(rf"\b{re.escape(attribute)}\s*=\s*(['\"])(.*?)\1", tag, re.I | re.S)
    if match:
        return html.unescape(match.group(2))
    match = re.search(rf"\b{re.escape(attribute)}\s*=\s*([^\s>]+)", tag, re.I)
    return html.unescape(match.group(1)) if match else None


def parse_page(source: str, original_url: str) -> dict[str, Any]:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(original_url).query)
    board_id = (query.get("board.id") or [""])[0]
    thread_id = (query.get("message.id") or [""])[0]
    breadcrumb = re.search(
        r'<span\b[^>]*class=["\']?navbar_text["\']?[^>]*>(.*?)</span>',
        source,
        re.I | re.S,
    )
    title_match = re.search(r"<title>(.*?)</title>", source, re.I | re.S)
    page_title = plain_text(title_match.group(1) if title_match else "")
    thread_title = plain_text(breadcrumb.group(1)) if breadcrumb else page_title.rsplit(" - ", 2)[0]
    anchors = list(re.finditer(r'<tr\s+id=["\']?M(\d+)["\']?\s*>', source, re.I))
    posts: list[dict[str, Any]] = []
    for sequence, anchor in enumerate(anchors):
        end = anchors[sequence + 1].start() if sequence + 1 < len(anchors) else len(source)
        chunk = source[anchor.start():end]
        post_id = anchor.group(1)
        user = re.search(
            r"view_profile(?:\.jsp)?\?user\.id=(\d+)[^>]*class=[\"']?auth_text[^>]*>(.*?)</a>",
            chunk,
            re.I | re.S,
        )
        if not user:
            continue
        author_id = user.group(1)
        author_name = plain_text(user.group(2))
        subject = re.search(
            r'<td\b[^>]*class=["\']?subjectbar["\']?[^>]*width=["\']?100%["\']?[^>]*>(.*?)</td>',
            chunk,
            re.I | re.S,
        )
        content_start = re.search(r'<td\b[^>]*class=["\']msg_text_cell["\'][^>]*>', chunk, re.I | re.S)
        date_start = re.search(r'<td\b[^>]*class=["\']msg_date_cell["\'][^>]*>', chunk, re.I | re.S)
        if not content_start or not date_start or date_start.start() <= content_start.end():
            continue
        content_html = chunk[content_start.end():date_start.start()]
        content_html = re.sub(r"</td>\s*</tr>\s*<tr>\s*$", "", content_html, flags=re.I | re.S).strip()
        content_html = re.split(
            r'<p>\s*<div\b[^>]*style=["\'][^"\']*height:\s*48[^"\']*["\']',
            content_html,
            maxsplit=1,
            flags=re.I,
        )[0].strip()
        date_chunk = chunk[date_start.end():]
        date = re.search(r'<span\s+class=["\']?date_text["\']?>(.*?)</span>', date_chunk, re.I | re.S)
        clock = re.search(r'<span\s+class=["\']?time_text["\']?>(.*?)</span>', date_chunk, re.I | re.S)
        reply = re.search(r"Reply\s*<a[^>]*>\s*(\d+)\s*</a>\s*of\s*(\d+)", chunk, re.I | re.S)
        posts.append(
            {
                "post_id": post_id,
                "thread_id": thread_id or post_id,
                "board_id": board_id,
                "thread_title": thread_title,
                "post_title": plain_text(subject.group(1)) if subject else thread_title,
                "content_html": content_html,
                "content_text": plain_text(content_html),
                "author_id": author_id,
                "author_name": author_name,
                "posted_at": parse_nsider_datetime(
                    plain_text(date.group(1)) if date else None,
                    plain_text(clock.group(1)) if clock else None,
                ),
                "reply_number": int(reply.group(1)) if reply else None,
                "reply_count": int(reply.group(2)) if reply else None,
                "sequence": sequence,
            }
        )
    return {"board_id": board_id, "thread_id": thread_id, "thread_title": thread_title, "posts": posts}


def cdx_query(url: str, *, collapse: str | None = None) -> bytes:
    params: list[tuple[str, str]] = [
        ("url", url),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode,mimetype,digest,length"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
    ]
    if collapse:
        params.append(("collapse", collapse))
    return request_bytes(f"{CDX_URL}?{urllib.parse.urlencode(params)}", timeout=300)


def load_cdx(cache: Path, seed: Path | None = None) -> list[dict[str, str]]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        if seed:
            shutil.copyfile(seed, cache)
        else:
            cache.write_bytes(cdx_query(FORUM_PREFIX + "*", collapse="urlkey"))
    rows = json.loads(cache.read_bytes())
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:] if row and row[0]]


def preserve_profile(root: Path, db: sqlite3.Connection, seed: Path | None = None) -> None:
    raw = root / "raw" / "profile" / f"{PROFILE_TIMESTAMP}--user-{USER_ID}.html.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if raw.exists():
        with gzip.open(raw, "rb") as handle:
            payload = handle.read()
    elif seed:
        payload = seed.read_bytes()
        with gzip.open(raw, "wb", compresslevel=9) as handle:
            handle.write(payload)
    else:
        snapshot = f"https://web.archive.org/web/{PROFILE_TIMESTAMP}id_/{PROFILE_URL}"
        payload = request_bytes(snapshot)
        with gzip.open(raw, "wb", compresslevel=9) as handle:
            handle.write(payload)
    source = decode_html(payload)
    profile = parse_profile(source)
    digest = hashlib.sha256(payload).hexdigest()
    db.execute(
        """INSERT OR REPLACE INTO profiles(
               user_id,username,registered_at,last_visited_at,total_posts,rank,
               capture_timestamp,original_url,raw_path,raw_sha256,parsed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            USER_ID, profile["username"], profile["registered_at"],
            profile["last_visited_at"], profile["total_posts"], profile["rank"],
            PROFILE_TIMESTAMP, PROFILE_URL, str(raw.relative_to(root)), digest, utc_now(),
        ),
    )
    set_meta(db, "identity_evidence", {
        "username": USERNAME, "user_id": USER_ID,
        "registered_at": profile["registered_at"], "profile_total_posts": profile["total_posts"],
        "capture_timestamp": PROFILE_TIMESTAMP,
    })
    db.commit()


def board_for_url(url: str) -> str:
    return (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("board.id") or [""])[0]


def message_id_for_url(url: str) -> int | None:
    value = (
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("message.id")
        or [""]
    )[0]
    return int(value) if value.isdigit() else None


def targeted_candidate_key(row: dict[str, str]) -> tuple[int, int, int, str]:
    """Put likely post-registration STARFOXA pages ahead of blind samples.

    Lithium message IDs increased with time within each board. The cutoffs are
    deliberately conservative bounds derived from dates parsed from preserved
    pages: Power On was near 9.5M and Star Fox near 70K when the account was
    registered in June 2005. Older thread roots with an explicit page remain a
    second tier because a later page can contain post-registration replies.
    Nothing is discarded; this only changes the resumable scan order.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(row["original"]).query)
    board = (query.get("board.id") or [""])[0].lower()
    message_id = message_id_for_url(row["original"])
    has_page = bool((query.get("page") or [""])[0])
    cutoffs = {"np_po": 9_500_000, "starfox": 70_000}
    cutoff = cutoffs.get(board)
    if cutoff is None or (message_id is not None and message_id >= cutoff):
        era_rank = 0
    elif has_page:
        era_rank = 1
    else:
        era_rank = 2
    board_rank = {"np_po": 0, "starfox": 1}.get(board, 2)
    page_rank = 1 if has_page else 0
    stable = hashlib.sha256(row["original"].encode()).hexdigest()
    return era_rank, board_rank, page_rank, stable


def candidates(
    db: sqlite3.Connection,
    rows: Iterable[dict[str, str]],
    boards: set[str],
    retry_failures: bool,
    strategy: str = "targeted",
) -> list[dict[str, str]]:
    attempted = {
        (row["timestamp"], row["original_url"]): row["result"]
        for row in db.execute("SELECT timestamp,original_url,result FROM page_attempts")
    }
    selected = []
    for row in rows:
        if row["timestamp"] < REGISTERED_TIMESTAMP:
            continue
        board = board_for_url(row["original"])
        if boards and board.lower() not in boards:
            continue
        previous = attempted.get((row["timestamp"], row["original"]))
        if previous and not (retry_failures and previous == "fetch_error"):
            continue
        selected.append(row)
    if strategy == "targeted":
        return sorted(selected, key=targeted_candidate_key)
    return sorted(selected, key=lambda row: hashlib.sha256(row["original"].encode()).hexdigest())


def save_page(
    db: sqlite3.Connection,
    root: Path,
    row: dict[str, str],
    payload: bytes,
) -> tuple[int, int]:
    key = hashlib.sha256(f"{row['timestamp']}\0{row['original']}".encode()).hexdigest()[:20]
    raw = root / "raw" / "pages" / f"{row['timestamp']}--{key}.html.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        with gzip.open(raw, "wb", compresslevel=9) as handle:
            handle.write(payload)
    raw_digest = hashlib.sha256(payload).hexdigest()
    parsed = parse_page(decode_html(payload), row["original"])
    matched = [post for post in parsed["posts"] if post["author_id"] == USER_ID]
    relative = str(raw.relative_to(root))
    snapshot = f"https://web.archive.org/web/{row['timestamp']}/{row['original']}"
    for post in matched:
        canonical = (
            "http://forums.nintendo.com/nintendo/board/message?"
            f"board.id={urllib.parse.quote(post['board_id'])}&message.id={post['post_id']}#M{post['post_id']}"
        )
        db.execute(
            """INSERT INTO posts(
                   post_id,thread_id,board_id,thread_title,post_title,content_html,
                   content_text,author_id,author_name,posted_at,reply_number,reply_count,
                   canonical_url,capture_timestamp,snapshot_url,raw_path,raw_sha256,recovered_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(post_id) DO UPDATE SET
                   thread_id=excluded.thread_id,board_id=excluded.board_id,
                   thread_title=excluded.thread_title,post_title=excluded.post_title,
                   content_html=excluded.content_html,content_text=excluded.content_text,
                   author_name=excluded.author_name,posted_at=excluded.posted_at,
                   reply_number=excluded.reply_number,reply_count=excluded.reply_count,
                   capture_timestamp=excluded.capture_timestamp,snapshot_url=excluded.snapshot_url,
                   raw_path=excluded.raw_path,raw_sha256=excluded.raw_sha256,
                   recovered_at=excluded.recovered_at""",
            (
                post["post_id"], post["thread_id"], post["board_id"], post["thread_title"],
                post["post_title"], post["content_html"], post["content_text"], post["author_id"],
                post["author_name"], post["posted_at"], post["reply_number"], post["reply_count"],
                canonical, row["timestamp"], snapshot, relative, raw_digest, utc_now(),
            ),
        )
        db.execute("DELETE FROM context_posts WHERE requested_post_id=?", (post["post_id"],))
        for context in parsed["posts"]:
            db.execute(
                """INSERT INTO context_posts(
                       requested_post_id,context_post_id,sequence,author_id,author_name,
                       posted_at,post_title,content_html,content_text
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    post["post_id"], context["post_id"], context["sequence"], context["author_id"],
                    context["author_name"], context["posted_at"], context["post_title"],
                    context["content_html"], context["content_text"],
                ),
            )
    db.execute(
        """INSERT OR REPLACE INTO page_attempts(
               timestamp,original_url,board_id,digest,cdx_length,result,raw_path,raw_sha256,
               parsed_post_count,matched_post_count,fetch_error,attempted_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)""",
        (
            row["timestamp"], row["original"], parsed["board_id"], row.get("digest"),
            int(row.get("length") or 0), "matched" if matched else "no_match", relative,
            raw_digest, len(parsed["posts"]), len(matched), utc_now(),
        ),
    )
    db.commit()
    return len(parsed["posts"]), len(matched)


def record_error(db: sqlite3.Connection, row: dict[str, str], error: Exception) -> None:
    db.execute(
        """INSERT OR REPLACE INTO page_attempts(
               timestamp,original_url,board_id,digest,cdx_length,result,parsed_post_count,
               matched_post_count,fetch_error,attempted_at
           ) VALUES(?,?,?,?,?,'fetch_error',0,0,?,?)""",
        (
            row["timestamp"], row["original"], board_for_url(row["original"]), row.get("digest"),
            int(row.get("length") or 0), f"{type(error).__name__}: {error}", utc_now(),
        ),
    )
    db.commit()


def export_jsonl(db: sqlite3.Connection, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for row in db.execute("SELECT * FROM posts ORDER BY posted_at,post_id"):
            output.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def status(db: sqlite3.Connection) -> dict[str, Any]:
    profile = db.execute("SELECT * FROM profiles WHERE user_id=?", (USER_ID,)).fetchone()
    attempts = dict(db.execute("SELECT result,COUNT(*) FROM page_attempts GROUP BY result"))
    posts = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    threads = db.execute("SELECT COUNT(DISTINCT thread_id) FROM posts").fetchone()[0]
    context = db.execute("SELECT COUNT(*) FROM context_posts").fetchone()[0]
    dates = db.execute("SELECT MIN(posted_at),MAX(posted_at) FROM posts").fetchone()
    return {
        "identity": dict(profile) if profile else None,
        "attempts": attempts,
        "posts": posts,
        "threads": threads,
        "context_rows": context,
        "first_post": dates[0],
        "last_post": dates[1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent / "data" / "official_nsider")
    parser.add_argument("--limit", type=int, default=100, help="maximum new thread-page attempts; 0 is unlimited")
    parser.add_argument("--boards", default=",".join(DEFAULT_BOARDS))
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument(
        "--strategy",
        choices=("targeted", "hash"),
        default="targeted",
        help="candidate ordering; targeted prioritizes likely post-registration pages",
    )
    parser.add_argument("--seed-profile-html", type=Path)
    parser.add_argument("--seed-message-cdx", type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--export-jsonl", type=Path)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    db = connect(args.root / "official_nsider.sqlite3")
    try:
        if args.status:
            print(json.dumps(status(db), indent=2, ensure_ascii=False))
            return
        preserve_profile(args.root, db, args.seed_profile_html)
        rows = load_cdx(args.root / "raw" / "cdx-messages.json", args.seed_message_cdx)
        boards = {board.strip().lower() for board in args.boards.split(",") if board.strip()}
        set_meta(db, "cdx_inventory_count", len(rows))
        set_meta(db, "scan_boards", sorted(boards))
        set_meta(db, "scan_strategy", args.strategy)
        work = candidates(db, rows, boards, args.retry_failures, args.strategy)
        if args.limit:
            work = work[: args.limit]
        for index, row in enumerate(work, 1):
            snapshot = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
            try:
                payload = request_bytes(snapshot)
                parsed, matched = save_page(db, args.root, row, payload)
                print(f"[{index}/{len(work)}] {board_for_url(row['original'])}: {parsed} posts, {matched} matches")
            except ArchiveUnavailable as error:
                set_meta(db, "archive_unavailable", {"at": utc_now(), "error": str(error)})
                db.commit()
                print(f"[{index}/{len(work)}] stopped: {error}")
                break
            except Exception as error:  # the error and URL remain queryable in SQLite
                record_error(db, row, error)
                print(f"[{index}/{len(work)}] {board_for_url(row['original'])}: {type(error).__name__}: {error}")
            if index < len(work) and args.delay:
                time.sleep(args.delay)
        set_meta(db, "last_run", status(db))
        if args.export_jsonl:
            export_jsonl(db, args.export_jsonl)
        else:
            export_jsonl(db, args.root / "posts.jsonl")
        print(json.dumps(status(db), indent=2, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
