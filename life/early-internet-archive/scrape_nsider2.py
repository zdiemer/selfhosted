#!/usr/bin/env python3
"""Resumable, evidence-preserving NSider2 post collector for StarFoxA."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

API_URL = "https://www.tapatalk.com/groups/nsider2/mobiquo/mobiquo.php"
BASE_URL = "https://www.tapatalk.com/groups/nsider2"
USER_ID = "3029"
USERNAME = "StarFoxA"
PAGE_SIZE = 50


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(value: Any) -> Any:
    if isinstance(value, xmlrpc.client.Binary):
        return value.data.decode("utf-8", "replace")
    if isinstance(value, xmlrpc.client.DateTime):
        return str(value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


class TapatalkClient:
    def __init__(self, raw_dir: Path, delay: float, retries: int = 5) -> None:
        self.raw_dir = raw_dir
        self.delay = delay
        self.retries = retries
        self.last_request = 0.0

    def call(self, method: str, params: tuple[Any, ...], raw_name: str) -> Any:
        target = self.raw_dir / f"{raw_name}.xml.gz"
        if target.exists():
            with gzip.open(target, "rb") as handle:
                response = handle.read()
            parsed, _ = xmlrpc.client.loads(response)
            return normalize(parsed[0])

        payload = xmlrpc.client.dumps(params, methodname=method, allow_none=True).encode()
        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "Content-Type": "text/xml",
                "User-Agent": "ZachDiemerPersonalArchive/1.0",
            },
        )
        for attempt in range(self.retries):
            elapsed = time.monotonic() - self.last_request
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            try:
                with urllib.request.urlopen(request, timeout=45) as handle:
                    response = handle.read()
                self.last_request = time.monotonic()
                target.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(target, "wb", compresslevel=9) as handle:
                    handle.write(response)
                parsed, _ = xmlrpc.client.loads(response)
                return normalize(parsed[0])
            except (OSError, urllib.error.URLError, xmlrpc.client.Error) as error:
                if attempt + 1 == self.retries:
                    raise RuntimeError(f"{method} failed after {self.retries} attempts") from error
                time.sleep((2**attempt) + random.random())
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
        CREATE TABLE IF NOT EXISTS pages (
            source TEXT NOT NULL,
            page INTEGER NOT NULL,
            item_count INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (source, page)
        );
        CREATE TABLE IF NOT EXISTS topics (
            topic_id TEXT PRIMARY KEY,
            forum_id TEXT,
            title TEXT,
            total_post_num INTEGER,
            topic_time TEXT,
            timestamp INTEGER,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            forum_id TEXT,
            forum_name TEXT,
            topic_id TEXT NOT NULL,
            topic_title TEXT,
            reply_number INTEGER,
            view_number INTEGER,
            post_title TEXT,
            post_content TEXT,
            short_content TEXT,
            author_id TEXT,
            author_name TEXT,
            post_time TEXT,
            timestamp INTEGER,
            canonical_url TEXT NOT NULL,
            sources TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS posts_timestamp_idx ON posts(timestamp);
        CREATE INDEX IF NOT EXISTS posts_topic_idx ON posts(topic_id);
        CREATE TABLE IF NOT EXISTS context_windows (
            anchor_post_id TEXT PRIMARY KEY,
            requested_post_id TEXT NOT NULL,
            topic_id TEXT NOT NULL,
            position INTEGER,
            total_post_num INTEGER,
            item_count INTEGER NOT NULL,
            raw_path TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_posts (
            requested_post_id TEXT NOT NULL,
            context_post_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            topic_id TEXT NOT NULL,
            author_id TEXT,
            author_name TEXT,
            post_time TEXT,
            timestamp INTEGER,
            post_content TEXT,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (requested_post_id, context_post_id)
        );
        CREATE INDEX IF NOT EXISTS context_posts_anchor_idx
            ON context_posts(requested_post_id, sequence);
        """
    )
    return db


def set_meta(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        """INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), utc_now()),
    )


def canonical_url(post: dict[str, Any]) -> str:
    return f"{BASE_URL}/viewtopic.php?p={post['post_id']}#p{post['post_id']}"


def save_post(db: sqlite3.Connection, post: dict[str, Any], source: str) -> bool:
    if str(post.get("post_author_id", "")) != USER_ID:
        return False
    existing = db.execute("SELECT sources FROM posts WHERE post_id=?", (post["post_id"],)).fetchone()
    sources = set(json.loads(existing["sources"])) if existing else set()
    sources.add(source)
    db.execute(
        """
        INSERT INTO posts(
            post_id, forum_id, forum_name, topic_id, topic_title, reply_number,
            view_number, post_title, post_content, short_content, author_id,
            author_name, post_time, timestamp, canonical_url, sources,
            fetched_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            forum_id=excluded.forum_id, forum_name=excluded.forum_name,
            topic_id=excluded.topic_id, topic_title=excluded.topic_title,
            reply_number=excluded.reply_number, view_number=excluded.view_number,
            post_title=excluded.post_title, post_content=excluded.post_content,
            short_content=excluded.short_content, author_id=excluded.author_id,
            author_name=excluded.author_name, post_time=excluded.post_time,
            timestamp=excluded.timestamp, canonical_url=excluded.canonical_url,
            sources=excluded.sources, fetched_at=excluded.fetched_at,
            raw_json=excluded.raw_json
        """,
        (
            post["post_id"], post.get("forum_id"), post.get("forum_name"),
            post["topic_id"], post.get("topic_title"), post.get("reply_number"),
            post.get("view_number"), post.get("post_title"), post.get("post_content"),
            post.get("short_content"), post.get("post_author_id"),
            post.get("post_author_name"), post.get("post_time"),
            int(post.get("timestamp") or 0), canonical_url(post),
            json.dumps(sorted(sources)), utc_now(),
            json.dumps(post, ensure_ascii=False, sort_keys=True),
        ),
    )
    return existing is None


def save_page(db: sqlite3.Connection, source: str, page: int, count: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO pages(source, page, item_count, fetched_at) VALUES (?, ?, ?, ?)",
        (source, page, count, utc_now()),
    )


def scrape_author_search(db: sqlite3.Connection, client: TapatalkClient) -> None:
    page = 1
    while True:
        result = client.call(
            "search",
            ({"showposts": 1, "page": page, "perpage": PAGE_SIZE, "userid": USER_ID},),
            f"author-search/page-{page:04d}",
        )
        posts = result.get("posts", [])
        total = int(result.get("total_post_num") or 0)
        added = sum(save_post(db, post, "author_search") for post in posts)
        save_page(db, "author_search", page, len(posts))
        set_meta(db, "api_search_total", total)
        db.commit()
        print(f"author search page {page}: {len(posts)} posts ({added} new)", flush=True)
        if not posts or page * PAGE_SIZE >= total:
            break
        page += 1


def scrape_started_topics(db: sqlite3.Connection, client: TapatalkClient) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    page = 1
    while True:
        result = client.call(
            "search",
            ({"showposts": 0, "page": page, "perpage": PAGE_SIZE, "userid": USER_ID},),
            f"started-topics/page-{page:04d}",
        )
        batch = result.get("topics", [])
        total = int(result.get("total_topic_num") or 0)
        for topic in batch:
            db.execute(
                """INSERT OR REPLACE INTO topics(
                       topic_id, forum_id, title, total_post_num, topic_time, timestamp, raw_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic["topic_id"], topic.get("forum_id"), topic.get("topic_title"),
                    int(topic.get("total_post_num") or 0), topic.get("post_time"),
                    int(topic.get("timestamp") or 0),
                    json.dumps(topic, ensure_ascii=False, sort_keys=True),
                ),
            )
        topics.extend(batch)
        save_page(db, "started_topics", page, len(batch))
        set_meta(db, "started_topic_total", total)
        db.commit()
        print(f"started topics page {page}: {len(batch)} topics", flush=True)
        if not batch or page * PAGE_SIZE >= total:
            break
        page += 1
    return topics


def recover_from_started_topics(
    db: sqlite3.Connection, client: TapatalkClient, topics: Iterable[dict[str, Any]]
) -> None:
    topics = list(topics)
    for topic_number, topic in enumerate(topics, 1):
        topic_id = str(topic["topic_id"])
        total = int(topic.get("total_post_num") or 0)
        for start in range(0, total, PAGE_SIZE):
            last = min(start + PAGE_SIZE - 1, max(total - 1, 0))
            result = client.call(
                "get_thread",
                (topic_id, start, last, False),
                f"started-threads/topic-{topic_id}/posts-{start:05d}-{last:05d}",
            )
            posts = result.get("posts", [])
            added = sum(save_post(db, post, "started_topic_recovery") for post in posts)
            save_page(db, f"thread:{topic_id}", start // PAGE_SIZE + 1, len(posts))
            db.commit()
            if added:
                print(f"topic {topic_id}: recovered {added} posts", flush=True)
        if topic_number % 25 == 0 or topic_number == len(topics):
            print(f"recovery progress: {topic_number}/{len(topics)} topics", flush=True)


def recover_from_known_threads(db: sqlite3.Connection, client: TapatalkClient) -> None:
    topic_ids = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT topic_id FROM posts ORDER BY CAST(topic_id AS INTEGER) DESC"
        )
    ]
    for topic_number, topic_id in enumerate(topic_ids, 1):
        page = 1
        while True:
            result = client.call(
                "search",
                ({
                    "showposts": 1,
                    "page": page,
                    "perpage": PAGE_SIZE,
                    "userid": USER_ID,
                    "threadid": topic_id,
                },),
                f"known-thread-search/topic-{topic_id}/page-{page:04d}",
            )
            posts = result.get("posts", [])
            total = int(result.get("total_post_num") or 0)
            added = sum(save_post(db, post, "thread_partition_recovery") for post in posts)
            save_page(db, f"thread_search:{topic_id}", page, len(posts))
            db.commit()
            if added:
                print(f"thread {topic_id}: recovered {added} posts", flush=True)
            if not posts or page * PAGE_SIZE >= total:
                break
            page += 1
        if topic_number % 100 == 0 or topic_number == len(topic_ids):
            print(
                f"thread-partition progress: {topic_number}/{len(topic_ids)} topics",
                flush=True,
            )


def capture_context(
    db: sqlite3.Connection, client: TapatalkClient, workers: int = 4
) -> None:
    completed = 0
    while True:
        rows = db.execute(
            """SELECT p.topic_id, p.post_id
               FROM posts p
               LEFT JOIN context_windows c ON c.anchor_post_id = p.post_id
               WHERE c.anchor_post_id IS NULL
               GROUP BY p.topic_id
               ORDER BY MAX(p.timestamp) DESC
               LIMIT ?""",
            (workers,),
        ).fetchall()
        if not rows:
            break

        def fetch(requested_post_id: str) -> tuple[str, str, dict[str, Any]]:
            raw_name = f"context/post-{requested_post_id}"
            worker_client = TapatalkClient(client.raw_dir, client.delay, client.retries)
            result = worker_client.call(
                "get_thread_by_post",
                (requested_post_id, PAGE_SIZE, False),
                raw_name,
            )
            return requested_post_id, raw_name, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(fetch, [row["post_id"] for row in rows]))

        for requested_post_id, raw_name, result in results:
            posts = result.get("posts", [])
            if not posts:
                # Record a vanished anchor so a resume does not loop forever.
                db.execute(
                    """INSERT OR REPLACE INTO context_windows(
                           anchor_post_id, requested_post_id, topic_id, position,
                           total_post_num, item_count, raw_path, fetched_at
                       ) VALUES (?, ?, '', NULL, NULL, 0, ?, ?)""",
                    (requested_post_id, f"raw/{raw_name}.xml.gz", utc_now()),
                )
            else:
                topic_id = str(result.get("topic_id") or posts[0].get("topic_id") or "")
                for sequence, post in enumerate(posts):
                    db.execute(
                        """INSERT OR REPLACE INTO context_posts(
                               requested_post_id, context_post_id, sequence, topic_id,
                               author_id, author_name, post_time, timestamp, post_content,
                               raw_json
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            requested_post_id, post["post_id"], sequence, topic_id,
                            post.get("post_author_id"), post.get("post_author_name"),
                            post.get("post_time"), int(post.get("timestamp") or 0),
                            post.get("post_content"),
                            json.dumps(post, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    if str(post.get("post_author_id", "")) == USER_ID:
                        save_post(db, post, "context_recovery")
                        db.execute(
                            """INSERT OR IGNORE INTO context_windows(
                                   anchor_post_id, requested_post_id, topic_id, position,
                                   total_post_num, item_count, raw_path, fetched_at
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                post["post_id"], requested_post_id, topic_id,
                                result.get("position"), result.get("total_post_num"),
                                len(posts), f"raw/{raw_name}.xml.gz", utc_now(),
                            ),
                        )
            completed += 1
        db.commit()
        if completed // 100 != (completed - len(results)) // 100:
            remaining = db.execute(
                """SELECT COUNT(*) FROM posts p LEFT JOIN context_windows c
                   ON c.anchor_post_id=p.post_id WHERE c.anchor_post_id IS NULL"""
            ).fetchone()[0]
            print(f"context progress: {completed} windows fetched, {remaining} posts uncovered", flush=True)


def fetch_profile(db: sqlite3.Connection, client: TapatalkClient) -> None:
    profile = client.call(
        "get_user_info", (xmlrpc.client.Binary(b""), USER_ID), "profile/user-3029"
    )
    set_meta(db, "profile", profile)
    set_meta(db, "profile_post_count", int(profile.get("post_count") or 0))
    db.commit()


def print_status(db: sqlite3.Connection) -> None:
    posts = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    context_mappings = db.execute("SELECT COUNT(*) FROM context_windows").fetchone()[0]
    context_windows = db.execute(
        "SELECT COUNT(DISTINCT requested_post_id) FROM context_windows"
    ).fetchone()[0]
    context_messages = db.execute(
        "SELECT COUNT(DISTINCT context_post_id) FROM context_posts"
    ).fetchone()[0]
    earliest = db.execute("SELECT post_time, post_id FROM posts ORDER BY timestamp LIMIT 1").fetchone()
    latest = db.execute("SELECT post_time, post_id FROM posts ORDER BY timestamp DESC LIMIT 1").fetchone()
    metadata = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM metadata")}
    print(json.dumps({
        "archived_posts": posts,
        "profile_post_count": metadata.get("profile_post_count"),
        "api_search_total": metadata.get("api_search_total"),
        "started_topic_total": metadata.get("started_topic_total"),
        "profile_count_gap": (
            metadata.get("profile_post_count") - posts
            if metadata.get("profile_post_count") is not None else None
        ),
        "context_mappings": context_mappings,
        "context_windows": context_windows,
        "distinct_context_messages": context_messages,
        "earliest": dict(earliest) if earliest else None,
        "latest": dict(latest) if latest else None,
    }, indent=2, ensure_ascii=False))


def export_jsonl(db: sqlite3.Connection, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in db.execute("SELECT * FROM posts ORDER BY timestamp, CAST(post_id AS INTEGER)"):
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    print(f"exported {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data/nsider2")
    parser.add_argument("--delay", type=float, default=0.5, help="minimum delay between live requests")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--export-jsonl", type=Path)
    parser.add_argument("--skip-recovery", action="store_true")
    parser.add_argument("--skip-thread-partitions", action="store_true")
    parser.add_argument("--skip-context", action="store_true")
    parser.add_argument("--context-workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = connect(args.data_dir / "posts.sqlite3")
    if args.status:
        print_status(db)
        return 0
    if args.export_jsonl:
        export_jsonl(db, args.export_jsonl)
        return 0

    client = TapatalkClient(args.data_dir / "raw", max(args.delay, 0.0))
    fetch_profile(db, client)
    scrape_author_search(db, client)
    topics = scrape_started_topics(db, client)
    if not args.skip_recovery:
        recover_from_started_topics(db, client, topics)
    if not args.skip_thread_partitions:
        recover_from_known_threads(db, client)
    if not args.skip_context:
        capture_context(db, client, max(1, args.context_workers))
    set_meta(db, "completed_at", utc_now())
    db.commit()
    print_status(db)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted; progress is safely resumable", file=sys.stderr)
        raise SystemExit(130)
