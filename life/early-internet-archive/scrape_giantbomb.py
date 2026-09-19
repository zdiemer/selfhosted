#!/usr/bin/env python3
"""Discover and parse StarFoxA artifacts from preserved Giant Bomb profiles."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URL = "https://www.giantbomb.com"
PERSONAL_PATH = re.compile(
    r"^/profile/starfoxa/(?P<slug>.+)/(?P<type>30|46|51)-(?P<id>\d+)/?$",
    re.I,
)
MODERN_PATH = re.compile(
    r"^/profile/starfoxa/(?P<type>lists|blog|images)/(?P<slug>.+)/(?P<id>\d+)/?$",
    re.I,
)
KIND_BY_TYPE = {
    "30": "blog",
    "46": "list",
    "51": "image",
    "lists": "list",
    "blog": "blog",
    "images": "image",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</(?:p|div|li|blockquote|h\d)\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in fragment.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def absolute_url(value: str) -> str:
    value = html.unescape(value.strip())
    if value.startswith("//"):
        return "https:" + value
    return urllib.parse.urljoin(BASE_URL + "/profile/starfoxa/", value)


def normalized_personal_url(url: str, kind: str, slug: str, legacy_id: str) -> str:
    slug = slug.strip("/")
    if kind == "list":
        return f"{BASE_URL}/profile/starfoxa/lists/{slug}/{legacy_id}/"
    if kind == "blog" and "/profile/starfoxa/blog/" in url.lower():
        return f"{BASE_URL}/profile/starfoxa/blog/{slug}/{legacy_id}/"
    type_id = {"blog": "30", "image": "51"}[kind]
    return f"{BASE_URL}/profile/starfoxa/{slug}/{type_id}-{legacy_id}/"


def parse_artifacts(source: str) -> list[dict[str, Any]]:
    artifacts: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_url, raw_label in re.findall(
        r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", source
    ):
        url = absolute_url(raw_url)
        path = urllib.parse.urlsplit(url).path
        match = PERSONAL_PATH.match(path) or MODERN_PATH.match(path)
        if not match:
            continue
        values = match.groupdict()
        kind = KIND_BY_TYPE[values["type"].lower()]
        legacy_id = values["id"]
        slug = values["slug"].strip("/").split("/")[-1]
        title = clean_text(raw_label) or slug.replace("_", " ").replace("-", " ")
        key = (kind, legacy_id)
        observed_url = urllib.parse.urlunsplit(
            (
                "https",
                "www.giantbomb.com",
                path.replace("/profile/StarFoxA/", "/profile/starfoxa/"),
                "",
                "",
            )
        )
        candidate = {
            "artifact_id": f"{kind}:{legacy_id}",
            "kind": kind,
            "legacy_id": legacy_id,
            "slug": slug,
            "title": title,
            "url": normalized_personal_url(url, kind, slug, legacy_id),
            "urls": [observed_url],
        }
        current = artifacts.get(key)
        if current is None:
            artifacts[key] = candidate
        else:
            current["urls"].append(observed_url)
            if len(candidate["title"]) > len(current["title"]):
                current["title"] = candidate["title"]
    for artifact in artifacts.values():
        artifact["urls"] = sorted(set(artifact["urls"]))
    return list(artifacts.values())


def parse_date(value: str) -> str:
    value = value.strip().replace("Sept.", "Sep.")
    for pattern in ("%B %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return value


def parse_reviews(source: str) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for chunk in re.split(r'<div id="div_shout_review_', source)[1:]:
        id_match = re.match(r'(\d+)">', chunk)
        if not id_match:
            continue
        review_id = id_match.group(1)
        chunk = chunk.split('<div id="div_shout_review_', 1)[0]
        body_match = re.search(r'<div class="pb-20">(.*?)</div>', chunk, flags=re.I | re.S)
        game_match = re.search(
            r'<table class="review">.*?<td class="va-t">\s*<a href="([^"]+)"',
            chunk,
            flags=re.I | re.S,
        )
        headline_match = re.search(
            r'<span class="f-14 bold">(.*?)</span>', chunk, flags=re.I | re.S
        )
        date_match = re.search(
            r'<span class="f-11 lh-8">(.*?)</span>', chunk, flags=re.I | re.S
        )
        rating_match = re.search(r'/star-(\d+)\.png', chunk, flags=re.I)
        if not body_match or not game_match or not headline_match:
            continue
        game_url = absolute_url(game_match.group(1))
        game_slug = urllib.parse.urlsplit(game_url).path.strip("/").split("/")[0]
        reviews.append(
            {
                "review_id": review_id,
                "game_title": game_slug.replace("-", " ").title(),
                "game_url": game_url,
                "headline": clean_text(headline_match.group(1)),
                "rating": int(rating_match.group(1)) if rating_match else None,
                "reviewed_at": parse_date(clean_text(date_match.group(1))) if date_match else "",
                "body": clean_text(body_match.group(1)),
                "canonical_url": f"{game_url}user-reviews/?review_id={review_id}",
            }
        )
    return reviews


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            legacy_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            urls_json TEXT NOT NULL DEFAULT '[]',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            snapshot_count INTEGER NOT NULL,
            raw_paths_json TEXT NOT NULL,
            parsed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            game_title TEXT NOT NULL,
            game_url TEXT NOT NULL,
            headline TEXT NOT NULL,
            rating INTEGER,
            reviewed_at TEXT NOT NULL,
            body TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            snapshot_count INTEGER NOT NULL,
            raw_paths_json TEXT NOT NULL,
            parsed_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(artifacts)")}
    if "urls_json" not in columns:
        db.execute("ALTER TABLE artifacts ADD COLUMN urls_json TEXT NOT NULL DEFAULT '[]'")
        db.commit()
    return db


def discover(
    db: sqlite3.Connection,
    sweep_db: sqlite3.Connection,
    sweep_root: Path,
) -> None:
    artifacts: dict[str, dict[str, Any]] = {}
    reviews: dict[str, dict[str, Any]] = {}
    rows = sweep_db.execute(
        """SELECT timestamp,raw_path FROM captures
           WHERE target_id='giantbomb-profile' AND raw_path IS NOT NULL
           ORDER BY timestamp"""
    ).fetchall()
    for row in rows:
        raw_path = row["raw_path"]
        with gzip.open(
            sweep_root / "raw" / raw_path,
            "rt",
            encoding="utf-8",
            errors="replace",
        ) as source:
            payload = source.read()
        for parsed in parse_artifacts(payload):
            record = artifacts.setdefault(
                parsed["artifact_id"],
                dict(parsed, first_seen=row["timestamp"], last_seen=row["timestamp"], raw_paths=[]),
            )
            record["first_seen"] = min(record["first_seen"], row["timestamp"])
            record["last_seen"] = max(record["last_seen"], row["timestamp"])
            record["raw_paths"].append(raw_path)
            record["urls"] = sorted(set(record["urls"] + parsed["urls"]))
            if len(parsed["title"]) > len(record["title"]):
                record["title"] = parsed["title"]
        for parsed in parse_reviews(payload):
            record = reviews.setdefault(
                parsed["review_id"],
                dict(parsed, first_seen=row["timestamp"], last_seen=row["timestamp"], raw_paths=[]),
            )
            record["first_seen"] = min(record["first_seen"], row["timestamp"])
            record["last_seen"] = max(record["last_seen"], row["timestamp"])
            record["raw_paths"].append(raw_path)

    now = utc_now()
    for record in artifacts.values():
        db.execute(
            """INSERT OR REPLACE INTO artifacts(
                   artifact_id,kind,legacy_id,slug,title,url,urls_json,first_seen,
                   last_seen,snapshot_count,raw_paths_json,parsed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["artifact_id"], record["kind"], record["legacy_id"],
                record["slug"], record["title"], record["url"],
                json.dumps(record["urls"]), record["first_seen"],
                record["last_seen"], len(record["raw_paths"]),
                json.dumps(sorted(set(record["raw_paths"]))), now,
            ),
        )
        sweep_db.execute(
            """INSERT OR REPLACE INTO targets(id,source,kind,url,attribution)
               VALUES(?,?,?,?,?)""",
            (
                f"giantbomb-discovered-{record['kind']}-{record['legacy_id']}",
                "giantbomb", f"user-{record['kind']}", record["url"], "confirmed",
            ),
        )
        for index, alias in enumerate(
            (url for url in record["urls"] if url != record["url"]), 1
        ):
            sweep_db.execute(
                """INSERT OR REPLACE INTO targets(id,source,kind,url,attribution)
                   VALUES(?,?,?,?,?)""",
                (
                    f"giantbomb-discovered-{record['kind']}-{record['legacy_id']}-alias-{index}",
                    "giantbomb", f"user-{record['kind']}", alias, "confirmed",
                ),
            )
    for record in reviews.values():
        db.execute(
            """INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["review_id"], record["game_title"], record["game_url"],
                record["headline"], record["rating"], record["reviewed_at"],
                record["body"], record["canonical_url"], record["first_seen"],
                record["last_seen"], len(record["raw_paths"]),
                json.dumps(sorted(set(record["raw_paths"]))), now,
            ),
        )
    db.commit()
    sweep_db.commit()


def export_jsonl(db: sqlite3.Connection, root: Path) -> None:
    for table, filename, order in (
        ("artifacts", "artifacts.jsonl", "kind,legacy_id"),
        ("reviews", "reviews.jsonl", "reviewed_at,review_id"),
    ):
        with (root / filename).open("w", encoding="utf-8") as output:
            for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                output.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def status(db: sqlite3.Connection) -> dict[str, Any]:
    return {
        "artifacts": db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
        "by_kind": {
            row["kind"]: row["count"]
            for row in db.execute(
                "SELECT kind,COUNT(*) count FROM artifacts GROUP BY kind ORDER BY kind"
            )
        },
        "reviews": db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
    }


def main() -> None:
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=project / "data" / "giantbomb")
    parser.add_argument(
        "--sweep-dir", type=Path, default=project / "data" / "source_sweep"
    )
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    db = connect(args.data_dir / "giantbomb.sqlite3")
    sweep_db = sqlite3.connect(args.sweep_dir / "source_sweep.sqlite3")
    sweep_db.row_factory = sqlite3.Row
    discover(db, sweep_db, args.sweep_dir)
    export_jsonl(db, args.data_dir)
    print(json.dumps(status(db), indent=2))
    sweep_db.close()
    db.close()


if __name__ == "__main__":
    main()
