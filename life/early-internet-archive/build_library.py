#!/usr/bin/env python3
"""Build the immutable catalog consumed by the archive frontend.

The source-specific databases and recovered media remain the preservation
source of truth.  This file creates a disposable, read-optimized projection so
the web application never writes to (or migrates) preserved material.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"\[\*\]", "\n• ", value)
    value = re.sub(
        r"\[/?(?:b|i|u|s|quote|code|center|size|color|font|sup|sub|list|url|iurl|img|spoilers)(?:=[^]]*)?\]",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"<br\s*/?>|</p\s*>|</li\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_date(value: str | None, unix_timestamp: int | None = None) -> str:
    if unix_timestamp:
        return datetime.fromtimestamp(unix_timestamp, timezone.utc).isoformat()
    if not value:
        return ""
    candidates = (
        "%Y%m%dT%H:%M:%S%z",
        "%Y%m%d%H%M%S",
        "%B %d, %Y at %I:%M %p",
        "%Y-%m-%d",
    )
    for fmt in candidates:
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def open_source(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def create_catalog(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            author TEXT NOT NULL,
            published_at TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            section TEXT NOT NULL,
            completeness TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX items_source_external_id ON items(source, external_id);
        CREATE INDEX items_source_date ON items(source, published_at DESC);
        CREATE INDEX items_date ON items(published_at DESC);
        CREATE TABLE context_messages (
            item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            external_id TEXT NOT NULL,
            author TEXT NOT NULL,
            published_at TEXT NOT NULL,
            body TEXT NOT NULL,
            is_owner INTEGER NOT NULL,
            PRIMARY KEY(item_id, external_id)
        );
        CREATE INDEX context_item_sequence ON context_messages(item_id, sequence);
        CREATE TABLE assets (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            original_url TEXT NOT NULL,
            media_path TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            item_ids_json TEXT NOT NULL
        );
        CREATE INDEX assets_source ON assets(source);
        CREATE TABLE source_stats (
            source TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            asset_count INTEGER NOT NULL,
            first_at TEXT NOT NULL,
            last_at TEXT NOT NULL,
            description TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE items_fts USING fts5(
            id UNINDEXED,
            title,
            body,
            section,
            tags,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    return db


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as payload:
        for chunk in iter(lambda: payload.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_asset(
    db: sqlite3.Connection,
    *,
    asset_id: str,
    source: str,
    original_url: str,
    media_path: str,
    mime_type: str,
    captured_at: str,
    sha256: str,
    byte_length: int,
    item_ids: Iterable[str] = (),
) -> None:
    """Add an asset while preserving all item links for duplicate payloads."""
    existing = db.execute(
        "SELECT item_ids_json FROM assets WHERE id=?", (asset_id,)
    ).fetchone()
    linked_items = set(json.loads(existing[0])) if existing else set()
    linked_items.update(item_ids)
    db.execute(
        """INSERT INTO assets(
               id,source,original_url,media_path,mime_type,captured_at,
               sha256,byte_length,item_ids_json
           ) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               item_ids_json=excluded.item_ids_json""",
        (
            asset_id,
            source,
            original_url,
            media_path,
            mime_type,
            captured_at,
            sha256,
            byte_length,
            json.dumps(sorted(linked_items), ensure_ascii=False),
        ),
    )


def add_item(
    db: sqlite3.Connection,
    *,
    item_id: str,
    source: str,
    kind: str,
    external_id: str,
    title: str,
    body: str,
    author: str,
    published_at: str,
    canonical_url: str,
    section: str = "",
    completeness: str = "full",
    tags: Iterable[str] = (),
    metadata: dict | None = None,
) -> None:
    normalized_tags = sorted(set(tags))
    db.execute(
        """INSERT INTO items(
               id,source,kind,external_id,title,body,author,published_at,
               canonical_url,section,completeness,tags_json,metadata_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id, source, kind, external_id, title.strip(), body.strip(), author,
            published_at, canonical_url, section, completeness,
            json.dumps(normalized_tags, ensure_ascii=False),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    db.execute(
        "INSERT INTO items_fts(id,title,body,section,tags) VALUES(?,?,?,?,?)",
        (item_id, title, body, section, " ".join(normalized_tags)),
    )


def import_nsider2(db: sqlite3.Connection, root: Path) -> bool:
    path = root / "nsider2" / "posts.sqlite3"
    if not path.exists():
        return False
    source = open_source(path)
    for row in source.execute("SELECT * FROM posts ORDER BY timestamp,post_id"):
        body = clean_text(row["post_content"])
        title = row["post_title"] or row["topic_title"] or f"Post {row['post_id']}"
        add_item(
            db,
            item_id=f"nsider2:{row['post_id']}",
            source="nsider2",
            kind="forum-post",
            external_id=row["post_id"],
            title=clean_text(title),
            body=body,
            author=row["author_name"] or "StarFoxA",
            published_at=normalize_date(row["post_time"], row["timestamp"]),
            canonical_url=row["canonical_url"],
            section=row["forum_name"] or "NSider2",
            metadata={
                "topic_id": row["topic_id"],
                "topic_title": row["topic_title"] or "",
                "reply_number": row["reply_number"],
                "view_number": row["view_number"],
                "recovery_sources": json.loads(row["sources"]),
            },
        )
    db.commit()
    for row in source.execute(
        """SELECT requested_post_id,context_post_id,sequence,author_name,
                  post_time,timestamp,post_content
           FROM context_posts ORDER BY requested_post_id,sequence"""
    ):
        db.execute(
            """INSERT OR IGNORE INTO context_messages(
                   item_id,sequence,external_id,author,published_at,body,is_owner
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                f"nsider2:{row['requested_post_id']}", row["sequence"],
                row["context_post_id"], row["author_name"] or "",
                normalize_date(row["post_time"], row["timestamp"]),
                clean_text(row["post_content"]),
                int((row["author_name"] or "").lower() == "starfoxa"),
            ),
        )
    source.close()
    db.commit()
    return True


def import_official_nsider(db: sqlite3.Connection, root: Path) -> bool:
    path = root / "official_nsider" / "official_nsider.sqlite3"
    if not path.exists():
        return False
    source = open_source(path)
    if source.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
        source.close()
        return False
    for row in source.execute("SELECT * FROM posts ORDER BY posted_at,post_id"):
        add_item(
            db,
            item_id=f"official_nsider:{row['post_id']}",
            source="official_nsider",
            kind="forum-post",
            external_id=row["post_id"],
            title=row["post_title"] or row["thread_title"] or f"Post {row['post_id']}",
            body=row["content_text"],
            author=row["author_name"] or "STARFOXA",
            published_at=normalize_date(row["posted_at"]),
            canonical_url=row["snapshot_url"],
            section=row["board_id"] or "Official NSider",
            metadata={
                "thread_id": row["thread_id"],
                "thread_title": row["thread_title"],
                "reply_number": row["reply_number"],
                "reply_count": row["reply_count"],
                "official_url": row["canonical_url"],
                "capture_timestamp": row["capture_timestamp"],
            },
        )
    db.commit()
    for row in source.execute(
        """SELECT requested_post_id,context_post_id,sequence,author_name,
                  posted_at,content_text
           FROM context_posts ORDER BY requested_post_id,sequence"""
    ):
        db.execute(
            """INSERT OR IGNORE INTO context_messages(
                   item_id,sequence,external_id,author,published_at,body,is_owner
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                f"official_nsider:{row['requested_post_id']}", row["sequence"],
                row["context_post_id"], row["author_name"] or "",
                normalize_date(row["posted_at"]), row["content_text"],
                int((row["author_name"] or "").upper() == "STARFOXA"),
            ),
        )
    source.close()
    db.commit()
    return True


def import_indienerds(db: sqlite3.Connection, root: Path) -> bool:
    path = root / "indienerds" / "indienerds.sqlite3"
    if not path.exists():
        return False
    source = open_source(path)
    for row in source.execute("SELECT * FROM articles ORDER BY published_at,post_id"):
        tags = json.loads(row["tags_json"]) + json.loads(row["categories_json"])
        add_item(
            db,
            item_id=f"indienerds:{row['post_id']}",
            source="indienerds",
            kind="review" if "review" in tags else "article",
            external_id=str(row["post_id"]),
            title=row["title"],
            body=row["content_text"],
            author=row["author_name"],
            published_at=normalize_date(row["published_at"]),
            canonical_url=row["snapshot_url"],
            section="IndieNerds",
            completeness=row["completeness"],
            tags=tags,
            metadata={"original_url": row["original_url"], "snapshot_timestamp": row["snapshot_timestamp"]},
        )
    for row in source.execute("SELECT * FROM assets WHERE local_path IS NOT NULL ORDER BY url"):
        digest = row["sha256"] or hashlib.sha256(row["url"].encode()).hexdigest()
        add_asset(
            db,
            asset_id=f"indienerds:{digest[:20]}",
            source="indienerds",
            original_url=row["url"],
            media_path=f"indienerds/media/{row['local_path']}",
            mime_type=row["mimetype"] or "application/octet-stream",
            captured_at=normalize_date(row["capture_timestamp"]),
            sha256=digest,
            byte_length=row["byte_length"] or 0,
            item_ids=(f"indienerds:{x}" for x in json.loads(row["post_ids_json"])),
        )
    source.close()
    db.commit()
    return True


def import_photobucket(db: sqlite3.Connection, root: Path) -> bool:
    path = root / "photobucket" / "photobucket.sqlite3"
    if not path.exists():
        return False
    source = open_source(path)
    rows = source.execute(
        """SELECT asset_path,digest,MIN(original_url) original_url,
                  MIN(timestamp) captured_at,MIN(mimetype) mimetype,
                  MAX(content_length) content_length
           FROM captures WHERE asset_path IS NOT NULL
           GROUP BY asset_path,digest ORDER BY asset_path"""
    )
    for row in rows:
        local = root / "photobucket" / "media" / row["asset_path"]
        sha256 = file_sha256(local) if local.exists() else ""
        size = local.stat().st_size if local.exists() else int(row["content_length"] or 0)
        add_asset(
            db,
            asset_id=f"photobucket:{(sha256 or row['digest'])[:20]}",
            source="photobucket",
            original_url=row["original_url"],
            media_path=f"photobucket/media/{row['asset_path']}",
            mime_type=row["mimetype"] or "application/octet-stream",
            captured_at=normalize_date(row["captured_at"]),
            sha256=sha256,
            byte_length=size,
        )
    source.close()
    db.commit()
    return True


def import_backloggd(db: sqlite3.Connection, root: Path) -> bool:
    path = root / "backloggd" / "backloggd.sqlite3"
    if not path.exists():
        return False
    source = open_source(path)
    if source.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0:
        source.close()
        return False
    for row in source.execute("SELECT * FROM reviews ORDER BY reviewed_at,review_id"):
        tags = [value for value in (row["play_status"], row["platform"]) if value]
        if row["release_year"]:
            tags.append(str(row["release_year"]))
        add_item(
            db,
            item_id=f"backloggd:{row['review_id']}",
            source="backloggd",
            kind="review",
            external_id=row["review_id"],
            title=row["title"],
            body=row["body"],
            author="starfoxa",
            published_at=normalize_date(row["reviewed_at"]),
            canonical_url=row["canonical_url"],
            section="Backloggd",
            tags=tags,
            metadata={
                "game_url": row["game_url"],
                "release_year": row["release_year"],
                "rating": row["rating"],
                "play_status": row["play_status"],
                "platform": row["platform"],
                "source_page": row["source_page"],
            },
        )
    source.close()
    db.commit()
    return True


def build_stats(db: sqlite3.Connection) -> None:
    labels = {
        "official_nsider": (
            "Official NSider",
            "Recovered posts from Nintendo's original NSider forums with page context.",
        ),
        "nsider2": ("NSider2", "Forum posts with surrounding conversation context."),
        "indienerds": ("IndieNerds", "Reviews, hands-on articles, and interviews."),
        "photobucket": ("PhotoBucket", "Recovered images referenced by contemporary posts."),
        "backloggd": ("Backloggd", "Game reviews recovered from the confirmed StarFoxA profile."),
    }
    sources = {row[0] for row in db.execute("SELECT DISTINCT source FROM items")}
    sources.update(row[0] for row in db.execute("SELECT DISTINCT source FROM assets"))
    for source in sorted(sources):
        item_count = db.execute(
            "SELECT COUNT(*) FROM items WHERE source=?", (source,)
        ).fetchone()[0]
        asset_count = db.execute("SELECT COUNT(*) FROM assets WHERE source=?", (source,)).fetchone()[0]
        first_at, last_at = db.execute(
            """SELECT COALESCE(MIN(value),''),COALESCE(MAX(value),'')
               FROM (
                   SELECT published_at AS value FROM items WHERE source=?
                   UNION ALL
                   SELECT captured_at AS value FROM assets WHERE source=?
               ) WHERE value!=''""",
            (source, source),
        ).fetchone()
        label, description = labels.get(source, (source, ""))
        db.execute(
            "INSERT INTO source_stats VALUES(?,?,?,?,?,?,?)",
            (source, label, item_count, asset_count, first_at, last_at, description),
        )


def build(root: Path, output: Path) -> None:
    root = root.resolve()
    output = output.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"archive root does not exist: {root}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    db: sqlite3.Connection | None = None
    try:
        db = create_catalog(temporary)
        imported = {
            "official_nsider": import_official_nsider(db, root),
            "nsider2": import_nsider2(db, root),
            "indienerds": import_indienerds(db, root),
            "photobucket": import_photobucket(db, root),
            "backloggd": import_backloggd(db, root),
        }
        sources = [name for name, present in imported.items() if present]
        if not sources:
            raise RuntimeError(f"no supported archive databases found beneath {root}")
        build_stats(db)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "sources": sources,
        }
        db.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            [(key, json.dumps(value)) for key, value in metadata.items()],
        )
        db.commit()
        db.execute("PRAGMA optimize")
        check = db.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"catalog integrity check failed: {check}")
        counts = {
            "items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "context_messages": db.execute("SELECT COUNT(*) FROM context_messages").fetchone()[0],
            "assets": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        }
        db.close()
        db = None
        os.replace(temporary, output)
    finally:
        if db is not None:
            db.close()
        temporary.unlink(missing_ok=True)
    print(json.dumps({"output": str(output), **counts, "sources": metadata["sources"]}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    build(args.root, args.output or args.root / "library.sqlite3")


if __name__ == "__main__":
    main()
