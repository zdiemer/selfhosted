#!/usr/bin/env python3
"""Inventory and recover StarFoxA PhotoBucket media from web archives."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import html
import io
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
COMMON_CRAWL_COLLECTIONS = "https://index.commoncrawl.org/collinfo.json"
COMMON_CRAWL_DATA = "https://data.commoncrawl.org/"
USER_AGENT = "ZachDiemerPersonalArchive/1.0"
NAMESPACES = (
    "i123.photobucket.com/albums/o307/StarFoxA/",
    "s123.photobucket.com/albums/o307/StarFoxA/",
    "smg.photobucket.com/user/StarFoxA/",
    "photobucket.com/user/StarFoxA/",
    "www.photobucket.com/user/StarFoxA/",
)
LEGACY_COMMON_CRAWL_COLLECTIONS = (
    "CC-MAIN-2008-2009",
    "CC-MAIN-2009-2010",
    "CC-MAIN-2012",
    "CC-MAIN-2013-20",
    "CC-MAIN-2013-48",
)
PHOTOBUCKET_SURT_PREFIXES = (
    b"com,photobucket,i123)/albums/o307/starfoxa/",
    b"com,photobucket,s123)/albums/o307/starfoxa/",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_bytes(
    url: str,
    timeout: int = 90,
    headers: dict[str, str] | None = None,
    retries: int = 5,
) -> bytes:
    merged = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged)
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
            source TEXT NOT NULL,
            collection TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            status INTEGER,
            mimetype TEXT,
            digest TEXT,
            content_length INTEGER,
            warc_filename TEXT,
            warc_offset INTEGER,
            warc_length INTEGER,
            asset_path TEXT,
            discovered_at TEXT NOT NULL,
            UNIQUE(source, collection, timestamp, original_url, digest)
        );
        CREATE INDEX IF NOT EXISTS captures_url_idx ON captures(original_url);
        CREATE INDEX IF NOT EXISTS captures_digest_idx ON captures(digest);
        CREATE TABLE IF NOT EXISTS index_checks (
            source TEXT NOT NULL,
            collection TEXT NOT NULL,
            namespace TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            error TEXT,
            PRIMARY KEY(source, collection, namespace)
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS referenced_urls (
            url TEXT PRIMARY KEY,
            mention_count INTEGER NOT NULL,
            reference_source TEXT NOT NULL,
            wayback_checked_at TEXT,
            wayback_error TEXT
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


def discover_nsider2_references(db: sqlite3.Connection, nsider2_db: Path) -> None:
    if not nsider2_db.exists():
        return
    source = sqlite3.connect(nsider2_db)
    counts: dict[str, int] = {}
    url_pattern = re.compile(r"https?://[^\s\[\]<>\"']*photobucket\.com/[^\s\[\]<>\"']+", re.I)
    for table in ("posts", "context_posts"):
        for (content,) in source.execute(
            f"SELECT post_content FROM {table} WHERE LOWER(post_content) LIKE '%photobucket%'"
        ):
            if not content:
                continue
            text = html.unescape(content).replace("\\/", "/")
            for url in url_pattern.findall(text):
                url = url.rstrip(".,);!?:")
                parsed = urllib.parse.urlsplit(url)
                if "/o307/starfoxa/" not in parsed.path.lower():
                    continue
                relative = parsed.path.lower().split("/o307/starfoxa/", 1)[1]
                if not relative or relative.endswith("/"):
                    continue
                normalized = urllib.parse.urlunsplit(("http", parsed.netloc.lower(), parsed.path, parsed.query, ""))
                counts[normalized] = counts.get(normalized, 0) + 1
    source.close()
    for url, count in counts.items():
        db.execute(
            """INSERT INTO referenced_urls(url,mention_count,reference_source)
               VALUES(?,?,'nsider2')
               ON CONFLICT(url) DO UPDATE SET mention_count=excluded.mention_count""",
            (url, count),
        )
    set_meta(db, "nsider2_referenced_url_count", len(counts))
    set_meta(db, "nsider2_photobucket_mentions", sum(counts.values()))
    db.commit()
    print(f"NSider2 references: {len(counts)} file URLs, {sum(counts.values())} mentions", flush=True)


def wayback_query(
    namespace: str,
    cache: Path,
    prefix: bool = True,
    timeout: int = 180,
    retries: int = 5,
) -> list[dict[str, Any]]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        raw = cache.read_bytes()
    else:
        query = urllib.parse.urlencode({
            "url": namespace + "*" if prefix else namespace,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "filter": "statuscode:200",
        })
        raw = request_bytes(f"{WAYBACK_CDX}?{query}", timeout=timeout, retries=retries)
        cache.write_bytes(raw)
    rows = json.loads(raw)
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def collect_wayback(db: sqlite3.Connection, raw_dir: Path) -> None:
    for namespace in NAMESPACES:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", namespace).strip("_")
        try:
            rows = wayback_query(namespace, raw_dir / "wayback" / f"{slug}.json")
            error = None
        except Exception as exc:
            rows = []
            error = f"{type(exc).__name__}: {exc}"
        for row in rows:
            db.execute(
                """INSERT OR IGNORE INTO captures(
                       source,collection,timestamp,original_url,status,mimetype,digest,
                       content_length,discovered_at
                   ) VALUES('wayback','',?,?,?,?,?,?,?)""",
                (
                    row["timestamp"], row["original"], int(row["statuscode"]),
                    row["mimetype"], row["digest"], int(row.get("length") or 0), utc_now(),
                ),
            )
        db.execute(
            """INSERT OR REPLACE INTO index_checks(
                   source,collection,namespace,result_count,checked_at,error
               ) VALUES('wayback','',?,?,?,?)""",
            (namespace, len(rows), utc_now(), error),
        )
        db.commit()
        print(f"Wayback {namespace}: {len(rows)} captures", flush=True)


def collect_wayback_references(
    db: sqlite3.Connection, raw_dir: Path, workers: int, delay: float = 0.25
) -> None:
    rows = db.execute(
        """SELECT url FROM referenced_urls
           WHERE wayback_checked_at IS NULL OR wayback_error IS NOT NULL
           ORDER BY url"""
    ).fetchall()
    urls = [row["url"] for row in rows]

    def fetch(url: str) -> tuple[str, list[dict[str, Any]], str | None]:
        cache = raw_dir / "wayback-exact" / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
        try:
            captures = wayback_query(url, cache, prefix=False, timeout=30, retries=1)
            return url, captures, None
        except Exception as exc:
            return url, [], f"{type(exc).__name__}: {exc}"
        finally:
            time.sleep(delay)

    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, url) for url in urls]
        for future in concurrent.futures.as_completed(futures):
            url, captures, error = future.result()
            for row in captures:
                db.execute(
                    """INSERT OR IGNORE INTO captures(
                           source,collection,timestamp,original_url,status,mimetype,digest,
                           content_length,discovered_at
                       ) VALUES('wayback','',?,?,?,?,?,?,?)""",
                    (
                        row["timestamp"], row["original"], int(row["statuscode"]),
                        row["mimetype"], row["digest"], int(row.get("length") or 0), utc_now(),
                    ),
                )
            db.execute(
                "UPDATE referenced_urls SET wayback_checked_at=?,wayback_error=? WHERE url=?",
                (utc_now(), error, url),
            )
            db.commit()
            checked += 1
            if captures:
                print(f"Wayback exact hit: {url} ({len(captures)} captures)", flush=True)
            if checked % 25 == 0 or checked == len(urls):
                print(f"Wayback exact URLs: {checked}/{len(urls)} checked", flush=True)


def common_crawl_query(collection: dict[str, Any], namespace: str) -> tuple[list[dict[str, Any]], str | None]:
    query = urllib.parse.urlencode({
        "url": namespace,
        "matchType": "prefix",
        "output": "json",
        "filter": "status:200",
        "collapse": "urlkey",
    })
    try:
        raw = request_bytes(f"{collection['cdx-api']}?{query}", timeout=15, retries=1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return [], None
        return [], f"HTTP {exc.code}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "url" in item:
            rows.append(item)
    return rows, None


def collect_common_crawl(db: sqlite3.Connection, raw_dir: Path, workers: int) -> None:
    catalog = raw_dir / "common-crawl" / "collinfo.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    if catalog.exists():
        collections = json.loads(catalog.read_bytes())
    else:
        catalog.write_bytes(request_bytes(COMMON_CRAWL_COLLECTIONS, timeout=60))
        collections = json.loads(catalog.read_bytes())
    completed = {
        (row["collection"], row["namespace"])
        for row in db.execute(
            """SELECT collection,namespace FROM index_checks
               WHERE source='common-crawl' AND error IS NULL"""
        )
    }
    tasks = [
        (collection, namespace)
        for collection in collections
        for namespace in NAMESPACES
        if (collection["id"], namespace) not in completed
    ]

    def fetch(task: tuple[dict[str, Any], str]) -> tuple[dict[str, Any], str, list[dict[str, Any]], str | None]:
        collection, namespace = task
        rows, error = common_crawl_query(collection, namespace)
        return collection, namespace, rows, error

    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            collection, namespace, rows, error = future.result()
            collection_id = collection["id"]
            for row in rows:
                db.execute(
                    """INSERT OR IGNORE INTO captures(
                           source,collection,timestamp,original_url,status,mimetype,digest,
                           content_length,warc_filename,warc_offset,warc_length,discovered_at
                       ) VALUES('common-crawl',?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        collection_id, row.get("timestamp", ""), row["url"],
                        int(row.get("status") or 0), row.get("mime-detected") or row.get("mime"),
                        row.get("digest"), int(row.get("length") or 0), row.get("filename"),
                        int(row.get("offset") or 0), int(row.get("length") or 0), utc_now(),
                    ),
                )
            db.execute(
                """INSERT OR REPLACE INTO index_checks(
                       source,collection,namespace,result_count,checked_at,error
                   ) VALUES('common-crawl',?,?,?,?,?)""",
                (collection_id, namespace, len(rows), utc_now(), error),
            )
            db.commit()
            checked += 1
            if rows:
                print(f"Common Crawl {collection_id} {namespace}: {len(rows)} captures", flush=True)
            if checked % 100 == 0 or checked == len(tasks):
                print(f"Common Crawl indexes: {checked}/{len(tasks)} checks", flush=True)


def range_bytes(url: str, start: int, end: int) -> bytes:
    return request_bytes(
        url,
        timeout=60,
        headers={"Range": f"bytes={start}-{end}"},
        retries=3,
    )


def remote_size(url: str) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers["Content-Length"])


def cluster_line_at(index_url: str, size: int, position: int) -> tuple[int, int, bytes]:
    start = max(0, position - 8192)
    end = min(size - 1, position + 8192)
    data = range_bytes(index_url, start, end)
    local = position - start
    line_start = data.rfind(b"\n", 0, local) + 1
    line_end = data.find(b"\n", local)
    if line_end < 0:
        raise ValueError("cluster index line exceeds lookup window")
    return start + line_start, start + line_end, data[line_start:line_end]


def find_cluster_predecessor(index_url: str, size: int, target: bytes) -> tuple[int, bytes]:
    low, high = 0, size
    best: tuple[int, bytes] | None = None
    while low < high:
        middle = (low + high) // 2
        line_start, line_end, line = cluster_line_at(index_url, size, middle)
        key = line.split(b"\t", 1)[0]
        if key <= target:
            best = (line_start, line)
            low = line_end + 1
        else:
            high = line_start
    if best is None:
        raise ValueError("no predecessor in cluster index")
    return best


def collect_legacy_common_crawl(db: sqlite3.Connection, raw_dir: Path) -> None:
    catalog = raw_dir / "common-crawl" / "collinfo.json"
    if catalog.exists():
        listed = json.loads(catalog.read_bytes())
        collections = [
            item["id"]
            for item in listed
            if (match := re.match(r"CC-MAIN-(\d{4})", item["id"]))
            and int(match.group(1)) <= 2016
        ]
    else:
        collections = list(LEGACY_COMMON_CRAWL_COLLECTIONS)
    completed = {
        row[0]
        for row in db.execute(
            """SELECT collection FROM index_checks
               WHERE source='common-crawl-legacy' AND error IS NULL"""
        )
    }
    pending = [collection for collection in collections if collection not in completed]
    for number, collection in enumerate(pending, 1):
        base = (
            "https://data.commoncrawl.org/cc-index/collections/"
            f"{collection}/indexes/"
        )
        try:
            index_url = base + "cluster.idx"
            size = remote_size(index_url)
            seen_blocks: set[tuple[str, int, int]] = set()
            collection_hits = 0
            for prefix in PHOTOBUCKET_SURT_PREFIXES:
                line_offset, _ = find_cluster_predecessor(index_url, size, prefix)
                cluster_lines = range_bytes(
                    index_url, line_offset, min(size - 1, line_offset + 8192)
                ).splitlines()[:2]
                for line in cluster_lines:
                    parts = line.split(b"\t")
                    if len(parts) < 4:
                        continue
                    shard = parts[1].decode()
                    offset, length = int(parts[2]), int(parts[3])
                    block = (shard, offset, length)
                    if block in seen_blocks:
                        continue
                    seen_blocks.add(block)
                    compressed = range_bytes(base + shard, offset, offset + length - 1)
                    for raw_line in gzip.decompress(compressed).splitlines():
                        lowered = raw_line.lower()
                        if not any(lowered.startswith(candidate) for candidate in PHOTOBUCKET_SURT_PREFIXES):
                            continue
                        try:
                            urlkey, timestamp, payload = raw_line.split(b" ", 2)
                            row = json.loads(payload)
                        except (ValueError, json.JSONDecodeError):
                            continue
                        db.execute(
                            """INSERT OR IGNORE INTO captures(
                                   source,collection,timestamp,original_url,status,mimetype,digest,
                                   content_length,warc_filename,warc_offset,warc_length,discovered_at
                               ) VALUES('common-crawl',?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                collection, timestamp.decode(), row["url"],
                                int(row.get("status") or 0), row.get("mime"), row.get("digest"),
                                int(row.get("length") or 0), row.get("filename"),
                                int(row.get("offset") or 0), int(row.get("length") or 0), utc_now(),
                            ),
                        )
                        collection_hits += 1
            error = None
        except Exception as exc:
            collection_hits = 0
            error = f"{type(exc).__name__}: {exc}"
        db.execute(
            """INSERT OR REPLACE INTO index_checks(
                   source,collection,namespace,result_count,checked_at,error
               ) VALUES('common-crawl-legacy',?,'StarFoxA album',?,?,?)""",
            (collection, collection_hits, utc_now(), error),
        )
        db.commit()
        print(
            f"Legacy Common Crawl {number}/{len(pending)} {collection}: "
            f"{collection_hits} captures" + (f" ({error})" if error else ""),
            flush=True,
        )


def safe_relative_name(url: str, digest: str) -> Path:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    markers = ("/StarFoxA/", "/starfoxa/")
    relative = None
    for marker in markers:
        if marker in path:
            relative = path.split(marker, 1)[1]
            break
    relative = relative or PurePosixPath(path).name or "unnamed"
    parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", part) for part in PurePosixPath(relative).parts]
    clean = Path(*[part for part in parts if part not in ("", ".", "..")])
    if not clean.name:
        clean = Path("unnamed")
    return clean.with_name(f"{clean.stem}--{digest[:10]}{clean.suffix.lower()}")


def extract_common_crawl_payload(row: sqlite3.Row) -> bytes:
    start = int(row["warc_offset"])
    length = int(row["warc_length"])
    blob = request_bytes(
        COMMON_CRAWL_DATA + row["warc_filename"],
        timeout=120,
        headers={"Range": f"bytes={start}-{start + length - 1}"},
    )
    record = gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
    if record.startswith(b"WARC/"):
        _, _, body = record.partition(b"\r\n\r\n")
        _, _, payload = body.partition(b"\r\n\r\n")
    else:
        _, _, body = record.partition(b"\n")
        _, _, payload = body.partition(b"\r\n\r\n")
    return payload


def looks_like_image(data: bytes) -> bool:
    return (
        data.startswith((b"GIF87a", b"GIF89a", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
        or data[:4] in (b"RIFF", b"II*\x00", b"MM\x00*")
    )


def download_assets(db: sqlite3.Connection, media_dir: Path) -> None:
    rows = db.execute(
        """SELECT * FROM captures
           WHERE status=200 AND mimetype LIKE 'image/%' AND digest IS NOT NULL
           ORDER BY timestamp"""
    ).fetchall()
    by_digest: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_digest.setdefault(row["digest"], []).append(row)
    for number, (digest, choices) in enumerate(by_digest.items(), 1):
        existing = next((row["asset_path"] for row in choices if row["asset_path"]), None)
        relative = Path(existing) if existing else safe_relative_name(choices[0]["original_url"], digest)
        target = media_dir / relative
        if not target.exists():
            errors = []
            for row in choices:
                try:
                    if row["source"] == "wayback":
                        replay = (
                            f"https://web.archive.org/web/{row['timestamp']}id_/"
                            f"{row['original_url']}"
                        )
                        payload = request_bytes(replay, timeout=120)
                    else:
                        payload = extract_common_crawl_payload(row)
                    if not looks_like_image(payload):
                        raise ValueError("payload is not a recognized image")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                    break
                except Exception as exc:
                    errors.append(f"{row['source']}:{type(exc).__name__}:{exc}")
            else:
                print(f"unable to recover {choices[0]['original_url']}: {'; '.join(errors)}", flush=True)
                continue
        sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        for row in choices:
            db.execute("UPDATE captures SET asset_path=? WHERE id=?", (str(relative), row["id"]))
        db.commit()
        print(f"asset {number}/{len(by_digest)}: {relative} sha256={sha256[:12]}", flush=True)
        time.sleep(0.25)


def write_manifest(db: sqlite3.Connection, output: Path) -> None:
    captures = [dict(row) for row in db.execute("SELECT * FROM captures ORDER BY original_url,timestamp")]
    checks = [dict(row) for row in db.execute("SELECT * FROM index_checks ORDER BY source,collection,namespace")]
    assets = [
        dict(row)
        for row in db.execute(
            """SELECT asset_path,digest,MIN(timestamp) first_capture,
                      COUNT(DISTINCT original_url) url_count
               FROM captures WHERE asset_path IS NOT NULL
               GROUP BY asset_path,digest ORDER BY asset_path"""
        )
    ]
    references = [
        dict(row)
        for row in db.execute(
            """SELECT r.*,
                      EXISTS(SELECT 1 FROM captures c
                             WHERE LOWER(c.original_url)=LOWER(r.url)) AS recovered
               FROM referenced_urls r ORDER BY url"""
        )
    ]
    output.write_text(
        json.dumps(
            {
                "generated_at": utc_now(),
                "assets": assets,
                "captures": captures,
                "references": references,
                "index_checks": checks,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def print_status(db: sqlite3.Connection) -> None:
    stats = {
        "capture_rows": db.execute("SELECT COUNT(*) FROM captures").fetchone()[0],
        "distinct_urls": db.execute("SELECT COUNT(DISTINCT original_url) FROM captures").fetchone()[0],
        "distinct_digests": db.execute("SELECT COUNT(DISTINCT digest) FROM captures WHERE digest IS NOT NULL").fetchone()[0],
        "recovered_assets": db.execute("SELECT COUNT(DISTINCT asset_path) FROM captures WHERE asset_path IS NOT NULL").fetchone()[0],
        "wayback_checks": db.execute("SELECT COUNT(*) FROM index_checks WHERE source='wayback'").fetchone()[0],
        "common_crawl_checks": db.execute("SELECT COUNT(*) FROM index_checks WHERE source='common-crawl'").fetchone()[0],
        "common_crawl_hits": db.execute("SELECT COUNT(*) FROM captures WHERE source='common-crawl'").fetchone()[0],
        "legacy_common_crawl_checks": db.execute(
            "SELECT COUNT(*) FROM index_checks WHERE source='common-crawl-legacy' AND error IS NULL"
        ).fetchone()[0],
        "referenced_file_urls": db.execute("SELECT COUNT(*) FROM referenced_urls").fetchone()[0],
        "referenced_urls_with_capture": db.execute(
            """SELECT COUNT(*) FROM referenced_urls r WHERE EXISTS(
                   SELECT 1 FROM captures c WHERE LOWER(c.original_url)=LOWER(r.url))"""
        ).fetchone()[0],
        "referenced_urls_missing": db.execute(
            """SELECT COUNT(*) FROM referenced_urls r WHERE NOT EXISTS(
                   SELECT 1 FROM captures c WHERE LOWER(c.original_url)=LOWER(r.url))"""
        ).fetchone()[0],
    }
    print(json.dumps(stats, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data/photobucket")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--nsider2-db",
        type=Path,
        default=Path(__file__).parent / "data/nsider2/posts.sqlite3",
    )
    parser.add_argument("--skip-common-crawl", action="store_true")
    parser.add_argument("--skip-common-crawl-api", action="store_true")
    parser.add_argument("--skip-wayback-exact", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    db = connect(args.data_dir / "photobucket.sqlite3")
    if args.status:
        print_status(db)
        return 0
    discover_nsider2_references(db, args.nsider2_db)
    collect_wayback(db, args.data_dir / "raw")
    if not args.skip_wayback_exact:
        collect_wayback_references(db, args.data_dir / "raw", min(max(1, args.workers), 6))
    if not args.skip_common_crawl:
        collect_legacy_common_crawl(db, args.data_dir / "raw")
        if not args.skip_common_crawl_api:
            collect_common_crawl(db, args.data_dir / "raw", max(1, args.workers))
    download_assets(db, args.data_dir / "media")
    set_meta(db, "completed_at", utc_now())
    db.commit()
    write_manifest(db, args.data_dir / "manifest.json")
    print_status(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
