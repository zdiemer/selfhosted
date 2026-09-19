#!/usr/bin/env python3
"""Inventory seeded personal-source URLs in Wayback and Common Crawl.

This is deliberately an evidence collector, not a source-specific parser. It
records exact index results and can freeze raw Common Crawl payloads. Once a
source has enough recoverable pages, a dedicated parser can promote attributed
records into the public catalog without weakening provenance.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import io
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
COMMON_CRAWL_COLLECTIONS = "https://index.commoncrawl.org/collinfo.json"
COMMON_CRAWL_DATA = "https://data.commoncrawl.org/"
USER_AGENT = "ZachDiemerPersonalArchive/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_bytes(url: str, timeout: int = 30, retries: int = 2, headers: dict[str, str] | None = None) -> bytes:
    merged = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
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
        CREATE TABLE IF NOT EXISTS identities (
            source TEXT PRIMARY KEY,
            handle TEXT NOT NULL,
            confidence TEXT NOT NULL,
            evidence TEXT NOT NULL,
            profile_url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS targets (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            url TEXT NOT NULL,
            attribution TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            collection TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL REFERENCES targets(id),
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            status INTEGER,
            mimetype TEXT,
            digest TEXT,
            warc_filename TEXT,
            warc_offset INTEGER,
            warc_length INTEGER,
            raw_path TEXT,
            discovered_at TEXT NOT NULL,
            UNIQUE(provider, collection, timestamp, original_url, digest)
        );
        CREATE INDEX IF NOT EXISTS captures_target_idx ON captures(target_id, timestamp);
        CREATE TABLE IF NOT EXISTS index_checks (
            provider TEXT NOT NULL,
            collection TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL,
            query_url TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            error TEXT,
            PRIMARY KEY(provider, collection, target_id, query_url)
        );
        """
    )
    return db


def load_seeds(db: sqlite3.Connection, path: Path) -> None:
    seeds = json.loads(path.read_text(encoding="utf-8"))
    db.executemany(
        """INSERT INTO identities(source,handle,confidence,evidence,profile_url)
           VALUES(:source,:handle,:confidence,:evidence,:profile_url)
           ON CONFLICT(source) DO UPDATE SET
             handle=excluded.handle,confidence=excluded.confidence,
             evidence=excluded.evidence,profile_url=excluded.profile_url""",
        seeds["identities"],
    )
    db.executemany(
        """INSERT INTO targets(id,source,kind,url,attribution)
           VALUES(:id,:source,:kind,:url,:attribution)
           ON CONFLICT(id) DO UPDATE SET
             source=excluded.source,kind=excluded.kind,url=excluded.url,
             attribution=excluded.attribution""",
        seeds["targets"],
    )
    db.commit()


def query_urls(url: str) -> list[str]:
    """Return exact-index URL variants without broadening into a domain crawl."""
    parsed = urllib.parse.urlsplit(url)
    hosts = {parsed.netloc.lower()}
    if parsed.netloc.lower().startswith("www."):
        hosts.add(parsed.netloc.lower()[4:])
    else:
        hosts.add("www." + parsed.netloc.lower())
    suffix = f"?{parsed.query}" if parsed.query else ""
    paths = {(parsed.path or "/") + suffix}
    if parsed.path.endswith("/"):
        paths.add(parsed.path.rstrip("/") + suffix)
    else:
        paths.add(parsed.path + "/" + suffix)
    return sorted({f"{host}{path}" for host in hosts for path in paths})


def parse_json_lines(raw: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("url"):
            rows.append(row)
    return rows


def annual_collections(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the oldest index in every year, plus every pre-2014 legacy index."""
    selected: dict[str, dict[str, Any]] = {}
    legacy: list[dict[str, Any]] = []
    for collection in reversed(collections):
        collection_id = collection["id"]
        year = collection_id.removeprefix("CC-MAIN-")[:4]
        if year.isdigit() and int(year) < 2014:
            legacy.append(collection)
        elif year.isdigit():
            selected.setdefault(year, collection)
    return legacy + [selected[year] for year in sorted(selected)]


def common_crawl_query(collection: dict[str, Any], url: str) -> tuple[list[dict[str, Any]], str | None]:
    query = urllib.parse.urlencode({"url": url, "matchType": "exact", "output": "json"})
    try:
        raw = request_bytes(f"{collection['cdx-api']}?{query}", timeout=20, retries=1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return [], None
        return [], f"HTTP {exc.code}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return parse_json_lines(raw), None


def collect_common_crawl(
    db: sqlite3.Connection,
    raw_dir: Path,
    workers: int,
    all_collections: bool = False,
    sources: list[str] | None = None,
) -> None:
    catalog_path = raw_dir / "common-crawl" / "collinfo.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    if not catalog_path.exists():
        shared_catalog = raw_dir.parents[1] / "photobucket" / "raw" / "common-crawl" / "collinfo.json"
        if shared_catalog.exists():
            catalog_path.write_bytes(shared_catalog.read_bytes())
        else:
            catalog_path.write_bytes(request_bytes(COMMON_CRAWL_COLLECTIONS, timeout=60))
    collections = json.loads(catalog_path.read_bytes())
    if not all_collections:
        collections = annual_collections(collections)
    completed = {
        (row["collection"], row["target_id"], row["query_url"])
        for row in db.execute(
            "SELECT collection,target_id,query_url FROM index_checks "
            "WHERE provider='common-crawl' AND error IS NULL"
        )
    }
    target_rows = db.execute("SELECT id,source,url FROM targets ORDER BY id").fetchall()
    if sources:
        target_rows = [target for target in target_rows if target["source"] in sources]
    tasks = [
        (collection, target["id"], query_url)
        for collection in collections
        for target in target_rows
        for query_url in query_urls(target["url"])
        if (collection["id"], target["id"], query_url) not in completed
    ]

    def fetch(task: tuple[dict[str, Any], str, str]) -> tuple[dict[str, Any], str, str, list[dict[str, Any]], str | None]:
        collection, target_id, query_url = task
        rows, error = common_crawl_query(collection, query_url)
        return collection, target_id, query_url, rows, error

    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            collection, target_id, query_url, rows, error = future.result()
            collection_id = collection["id"]
            for row in rows:
                db.execute(
                    """INSERT OR IGNORE INTO captures(
                           provider,collection,target_id,timestamp,original_url,status,
                           mimetype,digest,warc_filename,warc_offset,warc_length,discovered_at
                       ) VALUES('common-crawl',?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        collection_id, target_id, row.get("timestamp", ""), row["url"],
                        int(row.get("status") or 0), row.get("mime-detected") or row.get("mime"),
                        row.get("digest"), row.get("filename"), int(row.get("offset") or 0),
                        int(row.get("length") or 0), utc_now(),
                    ),
                )
            db.execute(
                """INSERT OR REPLACE INTO index_checks(
                       provider,collection,target_id,query_url,result_count,checked_at,error
                   ) VALUES('common-crawl',?,?,?,?,?,?)""",
                (collection_id, target_id, query_url, len(rows), utc_now(), error),
            )
            db.commit()
            checked += 1
            if rows:
                print(f"Common Crawl hit: {collection_id} {target_id} ({len(rows)})", flush=True)
            if checked % 50 == 0 or checked == len(tasks):
                print(f"Common Crawl checks: {checked}/{len(tasks)}", flush=True)


def wayback_query(url: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "url": url,
            "matchType": "exact",
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "filter": "statuscode:200",
            "collapse": "digest",
        }
    )
    raw = request_bytes(f"{WAYBACK_CDX}?{query}", timeout=45, retries=2)
    payload = json.loads(raw)
    if not payload:
        return []
    header = payload[0]
    return [dict(zip(header, row)) for row in payload[1:]]


def collect_wayback(
    db: sqlite3.Connection,
    delay: float = 1.0,
    sources: list[str] | None = None,
) -> None:
    completed = {
        row["target_id"]
        for row in db.execute(
            "SELECT target_id FROM index_checks WHERE provider='wayback' AND error IS NULL"
        )
    }
    targets = db.execute("SELECT id,source,url FROM targets ORDER BY id").fetchall()
    if sources:
        targets = [target for target in targets if target["source"] in sources]
    for number, target in enumerate(targets, 1):
        if target["id"] in completed:
            continue
        try:
            rows = wayback_query(target["url"])
            error = None
        except Exception as exc:
            rows = []
            error = f"{type(exc).__name__}: {exc}"
        for row in rows:
            db.execute(
                """INSERT OR IGNORE INTO captures(
                       provider,collection,target_id,timestamp,original_url,status,
                       mimetype,digest,discovered_at
                   ) VALUES('wayback','',?,?,?,?,?,?,?)""",
                (
                    target["id"], row["timestamp"], row["original"],
                    int(row.get("statuscode") or 0), row.get("mimetype"),
                    row.get("digest"), utc_now(),
                ),
            )
        db.execute(
            """INSERT OR REPLACE INTO index_checks(
                   provider,collection,target_id,query_url,result_count,checked_at,error
               ) VALUES('wayback','',?,?,?,?,?)""",
            (target["id"], target["url"], len(rows), utc_now(), error),
        )
        db.commit()
        print(
            f"Wayback {number}/{len(targets)} {target['id']}: {len(rows)}"
            + (f" ({error})" if error else ""),
            flush=True,
        )
        time.sleep(delay)


def collect_live(
    db: sqlite3.Connection,
    raw_dir: Path,
    delay: float = 0.25,
    sources: list[str] | None = None,
) -> None:
    """Freeze currently accessible seed pages without treating errors as loss."""
    completed = {
        row["target_id"]
        for row in db.execute(
            "SELECT target_id FROM index_checks WHERE provider='live' AND error IS NULL"
        )
    }
    targets = db.execute("SELECT id,source,url FROM targets ORDER BY id").fetchall()
    if sources:
        targets = [target for target in targets if target["source"] in sources]
    pending = [target for target in targets if target["id"] not in completed]
    for number, target in enumerate(pending, 1):
        request = urllib.request.Request(
            target["url"],
            headers={"User-Agent": f"Mozilla/5.0 {USER_AGENT}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
                status_code = response.status
                final_url = response.geturl()
                mimetype = response.headers.get_content_type()
            digest = hashlib.sha256(payload).hexdigest()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            suffix = ".xml.gz" if "xml" in mimetype else ".html.gz"
            relative = (
                Path("live")
                / target["id"]
                / f"{timestamp}--{digest[:16]}{suffix}"
            )
            output = raw_dir / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(output, "wb", compresslevel=9) as compressed:
                compressed.write(payload)
            db.execute(
                """INSERT OR IGNORE INTO captures(
                       provider,collection,target_id,timestamp,original_url,status,
                       mimetype,digest,raw_path,discovered_at
                   ) VALUES('live','',?,?,?,?,?,?,?,?)""",
                (
                    target["id"], timestamp, final_url, status_code, mimetype,
                    digest, str(relative), utc_now(),
                ),
            )
            result_count = 1
            error = None
        except urllib.error.HTTPError as exc:
            result_count = 0
            error = f"HTTP {exc.code}"
        except Exception as exc:
            result_count = 0
            error = f"{type(exc).__name__}: {exc}"
        db.execute(
            """INSERT OR REPLACE INTO index_checks(
                   provider,collection,target_id,query_url,result_count,checked_at,error
               ) VALUES('live','',?,?,?,?,?)""",
            (target["id"], target["url"], result_count, utc_now(), error),
        )
        db.commit()
        print(
            f"Live {number}/{len(pending)} {target['id']}: "
            + ("captured" if error is None else error),
            flush=True,
        )
        time.sleep(delay)


def extract_warc_payload(blob: bytes) -> bytes:
    record = gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
    _, separator, content = record.partition(b"\r\n\r\n")
    if not separator:
        _, _, content = record.partition(b"\n\n")
    _, separator, payload = content.partition(b"\r\n\r\n")
    return payload if separator else content


def download_wayback(
    db: sqlite3.Connection,
    raw_dir: Path,
    delay: float = 0.25,
) -> None:
    """Freeze every indexed Wayback response that has not been saved yet."""
    rows = db.execute(
        """SELECT * FROM captures
           WHERE provider='wayback' AND status=200 AND raw_path IS NULL
           ORDER BY target_id,timestamp"""
    ).fetchall()
    for number, row in enumerate(rows, 1):
        replay_url = (
            f"https://web.archive.org/web/{row['timestamp']}id_/"
            f"{row['original_url']}"
        )
        try:
            payload = request_bytes(replay_url, timeout=60, retries=3)
            digest = hashlib.sha256(payload).hexdigest()
            suffix = ".xml.gz" if "xml" in (row["mimetype"] or "") else ".html.gz"
            relative = (
                Path("wayback")
                / row["target_id"]
                / f"{row['timestamp']}--{digest[:16]}{suffix}"
            )
            target = raw_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(target, "wb", compresslevel=9) as output:
                output.write(payload)
            db.execute("UPDATE captures SET raw_path=? WHERE id=?", (str(relative), row["id"]))
            db.commit()
            print(f"Wayback payload {number}/{len(rows)}: {relative}", flush=True)
        except Exception as exc:
            print(
                f"Wayback payload failed {row['target_id']} {row['timestamp']}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(delay)


def download_common_crawl(db: sqlite3.Connection, raw_dir: Path) -> None:
    rows = db.execute(
        """SELECT * FROM captures
           WHERE provider='common-crawl' AND status=200 AND warc_filename IS NOT NULL
             AND raw_path IS NULL
           ORDER BY target_id,timestamp"""
    ).fetchall()
    for number, row in enumerate(rows, 1):
        try:
            start = int(row["warc_offset"])
            length = int(row["warc_length"])
            blob = request_bytes(
                COMMON_CRAWL_DATA + row["warc_filename"],
                timeout=120,
                retries=3,
                headers={"Range": f"bytes={start}-{start + length - 1}"},
            )
            payload = extract_warc_payload(blob)
            digest = hashlib.sha256(payload).hexdigest()
            relative = Path("common-crawl") / row["target_id"] / f"{row['timestamp']}--{digest[:16]}.html.gz"
            target = raw_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(target, "wb", compresslevel=9) as output:
                output.write(payload)
            db.execute("UPDATE captures SET raw_path=? WHERE id=?", (str(relative), row["id"]))
            db.commit()
            print(f"payload {number}/{len(rows)}: {relative}", flush=True)
        except Exception as exc:
            print(f"payload failed {row['target_id']}: {type(exc).__name__}: {exc}", flush=True)


def status(db: sqlite3.Connection) -> dict[str, Any]:
    return {
        "identities": db.execute("SELECT COUNT(*) FROM identities").fetchone()[0],
        "targets": db.execute("SELECT COUNT(*) FROM targets").fetchone()[0],
        "checks": db.execute("SELECT COUNT(*) FROM index_checks").fetchone()[0],
        "check_errors": db.execute("SELECT COUNT(*) FROM index_checks WHERE error IS NOT NULL").fetchone()[0],
        "captures": db.execute("SELECT COUNT(*) FROM captures").fetchone()[0],
        "captured_targets": db.execute("SELECT COUNT(DISTINCT target_id) FROM captures").fetchone()[0],
        "raw_payloads": db.execute("SELECT COUNT(*) FROM captures WHERE raw_path IS NOT NULL").fetchone()[0],
        "by_source": {
            row["source"]: {"targets": row["targets"], "captures": row["captures"]}
            for row in db.execute(
                """SELECT t.source,COUNT(DISTINCT t.id) targets,COUNT(c.id) captures
                   FROM targets t LEFT JOIN captures c ON c.target_id=t.id
                   GROUP BY t.source ORDER BY t.source"""
            )
        },
    }


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "source_sweep")
    parser.add_argument("--seeds", type=Path, default=root / "source_sweep_seeds.json")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--source",
        action="append",
        help="limit network checks to a source key; may be supplied more than once",
    )
    parser.add_argument("--all-collections", action="store_true")
    parser.add_argument("--skip-wayback", action="store_true")
    parser.add_argument("--skip-common-crawl", action="store_true")
    parser.add_argument("--live", action="store_true", help="freeze accessible seed URLs")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db = connect(args.data_dir / "source_sweep.sqlite3")
    load_seeds(db, args.seeds)
    if not args.status:
        if args.live:
            collect_live(db, args.data_dir / "raw", sources=args.source)
        if not args.skip_wayback:
            collect_wayback(db, sources=args.source)
        if not args.skip_common_crawl:
            collect_common_crawl(
                db,
                args.data_dir / "raw",
                max(1, args.workers),
                args.all_collections,
                args.source,
            )
        if args.download:
            download_wayback(db, args.data_dir / "raw")
            download_common_crawl(db, args.data_dir / "raw")
    print(json.dumps(status(db), indent=2))
    db.close()


if __name__ == "__main__":
    main()
