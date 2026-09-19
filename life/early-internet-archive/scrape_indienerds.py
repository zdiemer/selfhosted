#!/usr/bin/env python3
"""Preserve Zach Diemer's IndieNerds articles from the Internet Archive."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
SITE_PREFIX = "indienerds.com/wordpress/"
AUTHOR_ID = 30
AUTHOR_NAME = "Zach Diemer"
USER_AGENT = "ZachDiemerPersonalArchive/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_bytes(url: str, timeout: int = 90, retries: int = 5) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise
            if attempt + 1 == retries:
                raise
        except (OSError, urllib.error.URLError):
            if attempt + 1 == retries:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            status INTEGER NOT NULL,
            mimetype TEXT,
            digest TEXT,
            content_length INTEGER,
            raw_path TEXT,
            fetch_error TEXT,
            fetched_at TEXT,
            UNIQUE(timestamp, original_url, digest)
        );
        CREATE TABLE IF NOT EXISTS articles (
            post_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            published_at TEXT,
            categories_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            content_html TEXT NOT NULL,
            content_text TEXT NOT NULL,
            original_url TEXT NOT NULL,
            snapshot_timestamp TEXT NOT NULL,
            snapshot_url TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            completeness TEXT NOT NULL DEFAULT 'full',
            discovered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assets (
            url TEXT PRIMARY KEY,
            post_ids_json TEXT NOT NULL,
            capture_timestamp TEXT,
            mimetype TEXT,
            local_path TEXT,
            sha256 TEXT,
            byte_length INTEGER,
            fetch_error TEXT,
            fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return db


def set_meta(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        """INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), utc_now()),
    )


def cdx_inventory(cache: Path) -> list[dict[str, str]]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        raw = cache.read_bytes()
    else:
        query = urllib.parse.urlencode(
            {
                "url": SITE_PREFIX + "*",
                "output": "json",
                "fl": "timestamp,original,statuscode,mimetype,digest,length",
                "filter": ["statuscode:200", "mimetype:text/html"],
                "collapse": "urlkey",
            },
            doseq=True,
        )
        raw = request_bytes(f"{WAYBACK_CDX}?{query}", timeout=240)
        cache.write_bytes(raw)
    rows = json.loads(raw)
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def cdx_media_inventory(cache: Path) -> list[dict[str, str]]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        raw = cache.read_bytes()
    else:
        query = urllib.parse.urlencode(
            {
                "url": SITE_PREFIX + "wp-content/uploads/*",
                "output": "json",
                "fl": "timestamp,original,statuscode,mimetype,digest,length",
                "filter": "statuscode:200",
                "collapse": "urlkey",
            }
        )
        raw = request_bytes(f"{WAYBACK_CDX}?{query}", timeout=240)
        cache.write_bytes(raw)
    rows = json.loads(raw)
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def archive_capture(db: sqlite3.Connection, row: dict[str, str]) -> None:
    db.execute(
        """INSERT OR IGNORE INTO captures(
               timestamp,original_url,status,mimetype,digest,content_length
           ) VALUES(?,?,?,?,?,?)""",
        (
            row["timestamp"], row["original"], int(row["statuscode"]),
            row.get("mimetype"), row.get("digest"), int(row.get("length") or 0),
        ),
    )


def balanced_div(source: str, start_match: re.Match[str]) -> str:
    """Return the inner HTML of the div opened by start_match."""
    start = start_match.end()
    depth = 1
    for token in re.finditer(r"</?div\b[^>]*>", source[start:], re.I):
        tag = token.group(0)
        if tag.startswith("</"):
            depth -= 1
            if depth == 0:
                return source[start : start + token.start()]
        else:
            depth += 1
    return source[start:]


def plain_text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<br\s*/?>|</p\s*>|</li\s*>|</h[1-6]\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in fragment.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def content_asset_urls(content_html: str) -> list[str]:
    urls = set()
    for attr in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', content_html, re.I):
        url = html.unescape(attr)
        if "/wordpress/wp-content/uploads/" not in url:
            continue
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc.lower() in {"i0.wp.com", "i1.wp.com", "i2.wp.com"}:
            path = parsed.path.removeprefix("/indienerds.com")
            url = urllib.parse.urlunsplit(("http", "indienerds.com", path, "", ""))
        else:
            url = urllib.parse.urlunsplit((parsed.scheme or "http", parsed.netloc, parsed.path, "", ""))
        urls.add(url)
    return sorted(urls)


def parse_article(source: str) -> dict[str, Any] | None:
    outer = re.search(
        r'<(?:div|article)\b[^>]*\bid=["\']post-(\d+)["\'][^>]*\bclass=["\']([^"\']*)["\'][^>]*>',
        source,
        re.I,
    )
    if not outer:
        return None
    post_id = int(outer.group(1))
    article_tail = source[outer.end():]
    author_match = re.search(
        rf'<a\b[^>]*href=["\'][^"\']*[?&]author={AUTHOR_ID}(?:["\'&])[^>]*>\s*{re.escape(AUTHOR_NAME)}\s*</a>',
        article_tail,
        re.I,
    )
    if "author-starfoxa" not in outer.group(2).lower() and not author_match:
        return None
    title_match = re.search(
        r'<h[12]\b[^>]*class=["\'][^"\']*(?:entry-)?title[^"\']*["\'][^>]*>(.*?)</h[12]>',
        article_tail,
        re.I | re.S,
    )
    content_match = re.search(
        r'<div\b[^>]*class=["\'][^"\']*(?:post-content|entry-content)[^"\']*["\'][^>]*>',
        article_tail,
        re.I,
    )
    if not title_match or not content_match:
        return None
    content_start = outer.end() + content_match.start()
    section = source[content_start:]
    section_open = re.match(r'<div\b[^>]*>', section, re.I)
    assert section_open
    content_html = balanced_div(section, section_open).strip()
    content_html = re.split(r"<!--\s*Social Bookmarks BEGIN\s*-->", content_html, maxsplit=1, flags=re.I)[0].strip()
    title = plain_text(title_match.group(1))
    detail = re.search(
        rf"This entry was posted by\s*<a[^>]+author={AUTHOR_ID}[^>]*>(.*?)</a>\s*on\s*"
        r"([^,<]+(?:,\s*\d{4})?\s+at\s+[^,<]+)",
        source,
        re.I | re.S,
    )
    time_match = re.search(r'<time\b[^>]*datetime=["\']([^"\']+)["\']', article_tail, re.I)
    published = plain_text(detail.group(2)) if detail else (time_match.group(1) if time_match else None)
    classes = outer.group(2).split()
    categories = sorted({x.removeprefix("category-") for x in classes if x.startswith("category-")})
    tags = sorted(
        {x.removeprefix("post_tag-").removeprefix("tag-") for x in classes if x.startswith(("post_tag-", "tag-"))}
    )
    return {
        "post_id": post_id,
        "title": title,
        "published_at": published,
        "categories": categories,
        "tags": tags,
        "content_html": content_html,
        "content_text": plain_text(content_html),
        "asset_urls": content_asset_urls(content_html),
    }


def fetch_listing_fragments(
    db: sqlite3.Connection,
    rows: list[dict[str, str]],
    raw_dir: Path,
) -> None:
    """Preserve author excerpts when Wayback lacks an individual post capture."""
    listing_rows = []
    for row in rows:
        parsed = urllib.parse.urlsplit(row["original"])
        query = parsed.query
        if "wp-login.php" in parsed.path:
            continue
        if not query or re.search(r"(?:^|&)(?:m=\d{6}|paged=\d+)(?:&|$)", query):
            listing_rows.append(row)
    found: set[int] = set()
    for row in listing_rows:
        slug = hashlib.sha1(row["original"].encode()).hexdigest()[:12]
        relative = Path("listings") / f"{row['timestamp']}-{slug}.html.gz"
        target = raw_dir / relative
        try:
            if target.exists():
                payload = gzip.decompress(target.read_bytes())
            else:
                replay = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
                payload = request_bytes(replay, timeout=120)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(gzip.compress(payload, mtime=0))
        except Exception:
            continue
        source = payload.decode("utf-8", "replace")
        starts = list(
            re.finditer(
                r'<(?:div|article)\b[^>]*id=["\']post-(\d+)["\'][^>]*class=["\'][^"\']*author-starfoxa[^"\']*["\'][^>]*>',
                source,
                re.I,
            )
        )
        for match in starts:
            fragment = source[match.start():]
            article = parse_article(fragment)
            if not article:
                continue
            if not article["published_at"]:
                month_match = re.search(r"(?:^|&)m=(\d{4})(\d{2})(?:&|$)", urllib.parse.urlsplit(row["original"]).query)
                day_match = re.search(r'class=["\'][^"\']*day[^"\']*["\'][^>]*>(.*?)</p>', fragment, re.I | re.S)
                if month_match and day_match:
                    day_text = plain_text(day_match.group(1))
                    day_number = re.search(r"\d+", day_text)
                    if day_number:
                        article["published_at"] = (
                            f"{month_match.group(1)}-{month_match.group(2)}-{int(day_number.group(0)):02d}"
                        )
            found.add(article["post_id"])
            existing = db.execute(
                "SELECT completeness FROM articles WHERE post_id=?", (article["post_id"],)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE articles SET published_at=COALESCE(published_at,?) WHERE post_id=?",
                    (article["published_at"], article["post_id"]),
                )
                continue
            raw_sha256 = hashlib.sha256(payload).hexdigest()
            snapshot_url = f"https://web.archive.org/web/{row['timestamp']}/{row['original']}"
            db.execute(
                """INSERT INTO articles(
                       post_id,title,author_id,author_name,published_at,categories_json,tags_json,
                       content_html,content_text,original_url,snapshot_timestamp,snapshot_url,
                       raw_path,raw_sha256,completeness,discovered_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    article["post_id"], article["title"], AUTHOR_ID, AUTHOR_NAME,
                    article["published_at"], json.dumps(article["categories"]), json.dumps(article["tags"]),
                    article["content_html"], article["content_text"], row["original"], row["timestamp"],
                    snapshot_url, str(relative), raw_sha256, "excerpt", utc_now(),
                ),
            )
    set_meta(db, "listing_author_post_ids", sorted(found))
    db.commit()


def fetch_pages(db: sqlite3.Connection, rows: list[dict[str, str]], raw_dir: Path, workers: int) -> None:
    candidates: dict[int, dict[str, str]] = {}
    for row in rows:
        archive_capture(db, row)
        match = re.search(r"[?&]p=(\d+)(?:&|$)", html.unescape(row["original"]))
        if match and "cpage=" not in row["original"]:
            candidates.setdefault(int(match.group(1)), row)
    db.commit()
    print(f"CDX inventory: {len(rows)} HTML URLs; {len(candidates)} article candidates", flush=True)

    def fetch(item: tuple[int, dict[str, str]]) -> tuple[int, dict[str, str], bytes | None, str | None]:
        post_id, row = item
        relative = Path("pages") / f"{post_id}-{row['timestamp']}.html.gz"
        target = raw_dir / relative
        if target.exists():
            return post_id, row, gzip.decompress(target.read_bytes()), None
        replay = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        try:
            payload = request_bytes(replay, timeout=120)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(gzip.compress(payload, mtime=0))
            return post_id, row, payload, None
        except Exception as exc:
            return post_id, row, None, f"{type(exc).__name__}: {exc}"

    asset_posts: dict[str, set[int]] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in sorted(candidates.items())]
        for future in concurrent.futures.as_completed(futures):
            post_id, row, payload, error = future.result()
            done += 1
            relative = Path("pages") / f"{post_id}-{row['timestamp']}.html.gz"
            db.execute(
                """UPDATE captures SET raw_path=?,fetch_error=?,fetched_at=?
                   WHERE timestamp=? AND original_url=? AND digest=?""",
                (str(relative) if payload else None, error, utc_now(), row["timestamp"], row["original"], row["digest"]),
            )
            if payload:
                source = payload.decode("utf-8", "replace")
                article = parse_article(source)
                if article:
                    raw_sha256 = hashlib.sha256(payload).hexdigest()
                    snapshot_url = f"https://web.archive.org/web/{row['timestamp']}/{row['original']}"
                    db.execute(
                        """INSERT INTO articles(
                               post_id,title,author_id,author_name,published_at,categories_json,tags_json,
                               content_html,content_text,original_url,snapshot_timestamp,snapshot_url,
                               raw_path,raw_sha256,completeness,discovered_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(post_id) DO UPDATE SET
                               title=excluded.title,published_at=excluded.published_at,
                               categories_json=excluded.categories_json,tags_json=excluded.tags_json,
                               content_html=excluded.content_html,content_text=excluded.content_text,
                               original_url=excluded.original_url,snapshot_timestamp=excluded.snapshot_timestamp,
                               snapshot_url=excluded.snapshot_url,raw_path=excluded.raw_path,
                               raw_sha256=excluded.raw_sha256,completeness=excluded.completeness""",
                        (
                            article["post_id"], article["title"], AUTHOR_ID, AUTHOR_NAME,
                            article["published_at"], json.dumps(article["categories"]),
                            json.dumps(article["tags"]), article["content_html"], article["content_text"],
                            row["original"], row["timestamp"], snapshot_url, str(relative), raw_sha256,
                            "full", utc_now(),
                        ),
                    )
                    for url in article["asset_urls"]:
                        asset_posts.setdefault(url, set()).add(article["post_id"])
            db.commit()
            if done % 25 == 0 or done == len(candidates):
                count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
                print(f"pages {done}/{len(candidates)}; Zach articles {count}", flush=True)

    for url, post_ids in asset_posts.items():
        db.execute(
            """INSERT INTO assets(url,post_ids_json) VALUES(?,?)
               ON CONFLICT(url) DO UPDATE SET post_ids_json=excluded.post_ids_json""",
            (url, json.dumps(sorted(post_ids))),
        )
    db.commit()


def fetch_author_index_fragments(db: sqlite3.Connection, raw_dir: Path) -> None:
    """Record post IDs visible in author index even when full pages were not captured."""
    url = f"http://indienerds.com/wordpress/?author={AUTHOR_ID}"
    query = urllib.parse.urlencode(
        {"url": url, "output": "json", "fl": "timestamp,original,statuscode,mimetype,digest,length", "filter": "statuscode:200"}
    )
    cache = raw_dir / "cdx-author.json"
    raw = cache.read_bytes() if cache.exists() else request_bytes(f"{WAYBACK_CDX}?{query}", timeout=180)
    if not cache.exists():
        cache.write_bytes(raw)
    rows = json.loads(raw)
    if len(rows) < 2:
        return
    header = rows[0]
    captures = [dict(zip(header, x)) for x in rows[1:]]
    post_ids: set[int] = set()
    for row in captures:
        replay = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        target = raw_dir / "author" / f"author-30-{row['timestamp']}.html.gz"
        if target.exists():
            payload = gzip.decompress(target.read_bytes())
        else:
            payload = request_bytes(replay, timeout=120)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(gzip.compress(payload, mtime=0))
        source = payload.decode("utf-8", "replace")
        post_ids.update(
            int(x)
            for x in re.findall(
                r'<div\b[^>]*id=["\']post-(\d+)["\'][^>]*class=["\'][^"\']*author-starfoxa[^"\']*["\']',
                source,
                re.I,
            )
        )
    full = {row[0] for row in db.execute("SELECT post_id FROM articles WHERE completeness='full'")}
    set_meta(db, "author_index_post_ids", sorted(post_ids))
    set_meta(db, "author_index_without_full_capture", sorted(post_ids - full))
    db.commit()


def sync_asset_index(db: sqlite3.Connection) -> None:
    asset_posts: dict[str, list[int]] = {}
    for row in db.execute("SELECT post_id,content_html FROM articles"):
        for url in content_asset_urls(row["content_html"]):
            asset_posts.setdefault(url, []).append(row["post_id"])
    if asset_posts:
        placeholders = ",".join("?" for _ in asset_posts)
        db.execute(f"DELETE FROM assets WHERE url NOT IN ({placeholders})", tuple(asset_posts))
    for url, post_ids in asset_posts.items():
        db.execute(
            """INSERT INTO assets(url,post_ids_json) VALUES(?,?)
               ON CONFLICT(url) DO UPDATE SET post_ids_json=excluded.post_ids_json""",
            (url, json.dumps(sorted(post_ids))),
        )
    db.commit()


def safe_asset_name(url: str) -> Path:
    parsed = urllib.parse.urlsplit(url)
    marker = "/wordpress/wp-content/uploads/"
    relative = parsed.path.split(marker, 1)[-1]
    parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", x) for x in Path(relative).parts if x not in ("", ".", "..")]
    return Path(*parts)


def fetch_assets(
    db: sqlite3.Connection,
    media_dir: Path,
    workers: int,
    media_captures: list[dict[str, str]],
) -> None:
    rows = db.execute("SELECT url,post_ids_json FROM assets WHERE local_path IS NULL ORDER BY url").fetchall()

    # SQLite connections are not thread-safe, so resolve timestamps before submitting.
    captures_by_path: dict[str, list[dict[str, str]]] = {}
    for capture in media_captures:
        path = urllib.parse.urlsplit(capture["original"]).path.lower()
        captures_by_path.setdefault(path, []).append(capture)
    jobs = []
    for row in rows:
        post_id = json.loads(row["post_ids_json"])[0]
        article_timestamp = db.execute("SELECT snapshot_timestamp FROM articles WHERE post_id=?", (post_id,)).fetchone()[0]
        path = urllib.parse.urlsplit(row["url"]).path.lower()
        choices = captures_by_path.get(path, [])
        capture = min(choices, key=lambda x: abs(int(x["timestamp"]) - int(article_timestamp))) if choices else None
        jobs.append((dict(row), article_timestamp, capture))

    def fetch_job(job: tuple[dict[str, Any], str, dict[str, str] | None]) -> tuple[str, bytes | None, str | None, str]:
        row, article_timestamp, capture = job
        timestamp = capture["timestamp"] if capture else article_timestamp
        original = capture["original"] if capture else row["url"]
        existing = media_dir / safe_asset_name(row["url"])
        if existing.exists():
            return row["url"], existing.read_bytes(), None, timestamp
        replay = f"https://web.archive.org/web/{timestamp}id_/{original}"
        try:
            payload = request_bytes(replay, timeout=120)
            if payload.lstrip().startswith((b"<!DOCTYPE html", b"<html")):
                raise ValueError("archive replay returned HTML instead of media")
            return row["url"], payload, None, timestamp
        except Exception as exc:
            return row["url"], None, f"{type(exc).__name__}: {exc}", timestamp

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, result in enumerate(pool.map(fetch_job, jobs), 1):
            url, payload, error, timestamp = result
            local_path = sha256 = mimetype = None
            if payload:
                target = media_dir / safe_asset_name(url)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                local_path = str(target.relative_to(media_dir))
                sha256 = hashlib.sha256(payload).hexdigest()
                suffix = target.suffix.lower()
                mimetype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}.get(suffix)
            db.execute(
                """UPDATE assets SET capture_timestamp=?,mimetype=?,local_path=?,sha256=?,
                   byte_length=?,fetch_error=?,fetched_at=? WHERE url=?""",
                (timestamp, mimetype, local_path, sha256, len(payload) if payload else None, error, utc_now(), url),
            )
            db.commit()
            print(f"asset {number}/{len(jobs)}: {'saved' if payload else 'missing'} {url}", flush=True)


def export(db: sqlite3.Connection, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        for row in db.execute("SELECT * FROM articles ORDER BY published_at,post_id"):
            item = dict(row)
            item["categories"] = json.loads(item.pop("categories_json"))
            item["tags"] = json.loads(item.pop("tags_json"))
            item["assets"] = [
                dict(asset)
                for asset in db.execute(
                    "SELECT * FROM assets WHERE EXISTS (SELECT 1 FROM json_each(post_ids_json) WHERE value=?) ORDER BY url",
                    (row["post_id"],),
                )
            ]
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def print_status(db: sqlite3.Connection) -> None:
    stats = {
        "articles": db.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        "words": sum(len(row[0].split()) for row in db.execute("SELECT content_text FROM articles")),
        "assets_referenced": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        "assets_recovered": db.execute("SELECT COUNT(*) FROM assets WHERE local_path IS NOT NULL").fetchone()[0],
        "assets_missing": db.execute("SELECT COUNT(*) FROM assets WHERE local_path IS NULL").fetchone()[0],
        "raw_pages": db.execute("SELECT COUNT(*) FROM captures WHERE raw_path IS NOT NULL").fetchone()[0],
    }
    for key, value in stats.items():
        print(f"{key}: {value}")
    for row in db.execute("SELECT post_id,published_at,title FROM articles ORDER BY post_id"):
        print(f"{row['post_id']}\t{row['published_at'] or ''}\t{row['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent / "data" / "indienerds")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--skip-assets", action="store_true")
    args = parser.parse_args()
    db = connect(args.root / "indienerds.sqlite3")
    if args.status:
        print_status(db)
        return
    rows = cdx_inventory(args.root / "raw" / "cdx-html.json")
    set_meta(db, "site", "IndieNerds")
    set_meta(db, "author_id", AUTHOR_ID)
    set_meta(db, "author_name", AUTHOR_NAME)
    set_meta(db, "cdx_html_url_count", len(rows))
    db.commit()
    fetch_pages(db, rows, args.root / "raw", args.workers)
    fetch_listing_fragments(db, rows, args.root / "raw")
    fetch_author_index_fragments(db, args.root / "raw")
    sync_asset_index(db)
    if not args.skip_assets:
        media_rows = cdx_media_inventory(args.root / "raw" / "cdx-media.json")
        set_meta(db, "cdx_media_url_count", len(media_rows))
        db.commit()
        fetch_assets(db, args.root / "media", args.workers, media_rows)
    export(db, args.root / "articles.jsonl")
    print_status(db)


if __name__ == "__main__":
    main()
