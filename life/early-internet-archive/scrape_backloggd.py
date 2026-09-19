#!/usr/bin/env python3
"""Preserve and parse the public Backloggd review history for StarFoxA."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URL = "https://backloggd.com"
PROFILE_URL = f"{BASE_URL}/u/starfoxa/"
REVIEWS_URL = f"{BASE_URL}/u/starfoxa/reviews/"
USER_AGENT = "ZachDiemerPersonalArchive/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_bytes(url: str, timeout: int = 45, retries: int = 4) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Mozilla/5.0 {USER_AGENT}"},
    )
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
        CREATE TABLE IF NOT EXISTS profile (
            username TEXT PRIMARY KEY,
            profile_url TEXT NOT NULL,
            bio TEXT NOT NULL,
            total_games INTEGER,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pages (
            page INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            raw_path TEXT,
            raw_sha256 TEXT,
            review_count INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            game_url TEXT NOT NULL,
            release_year INTEGER,
            rating REAL,
            play_status TEXT NOT NULL,
            platform TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            body TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            source_page INTEGER NOT NULL REFERENCES pages(page),
            parsed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reviews_date_idx ON reviews(reviewed_at);
        """
    )
    return db


def text_content(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</(?:p|div|li|blockquote)\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in fragment.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def match_text(pattern: str, source: str, default: str = "") -> str:
    match = re.search(pattern, source, flags=re.I | re.S)
    return text_content(match.group(1)) if match else default


def parse_profile(source: str) -> dict[str, Any]:
    bio = match_text(r'<span id="bio-body">(.*?)</span>', source)
    if not bio:
        bio = match_text(r"<h5[^>]*>\s*Bio\s*</h5>(.*?)(?:<h5|Personal Ratings)", source)
    total_text = match_text(r"<h1[^>]*>\s*([0-9,]+)\s*</h1>\s*</a>\s*<h4>\s*Games Played", source)
    if not total_text:
        total_text = match_text(r"<h4[^>]*>\s*([0-9,]+)\s*</h4>\s*<p[^>]*>\s*Total Games Played", source)
    return {
        "username": "starfoxa",
        "profile_url": PROFILE_URL,
        "bio": bio,
        "total_games": int(total_text.replace(",", "")) if total_text else None,
    }


def parse_reviews(source: str, page: int) -> list[dict[str, Any]]:
    marker = '<div class="row mb-1 game-name">'
    chunks = source.split(marker)[1:]
    reviews: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk = chunk.split(marker, 1)[0]
        review_id_match = re.search(r'review_id="(\d+)"', chunk)
        if not review_id_match:
            continue
        review_id = review_id_match.group(1)
        title_match = re.search(
            r'<a href="([^"]+)">\s*<h3[^>]*>(.*?)</h3>', chunk, flags=re.I | re.S
        )
        body_match = re.search(
            rf'<div class="[^"]*card-text" id="collapseReview{review_id}">(.*?)</div>',
            chunk,
            flags=re.I | re.S,
        )
        canonical_match = re.search(
            rf'<a[^>]+href="(/u/starfoxa/review/{review_id}/?)"[^>]*>\s*Open review',
            chunk,
            flags=re.I | re.S,
        )
        if not title_match or not body_match or not canonical_match:
            continue
        rating_match = re.search(r'stars-top" style="width:\s*(\d+)%', chunk, flags=re.I)
        year_text = match_text(r'game-date[^>]*>\s*(\d{4})\s*</p>', chunk)
        reviews.append(
            {
                "review_id": review_id,
                "title": text_content(title_match.group(2)),
                "game_url": BASE_URL + html.unescape(title_match.group(1)),
                "release_year": int(year_text) if year_text else None,
                "rating": int(rating_match.group(1)) / 20 if rating_match else None,
                "play_status": match_text(r'play-type[^>]*>\s*(.*?)\s*</p>', chunk),
                "platform": match_text(r'review-platform[^>]*>\s*<p[^>]*>(.*?)</p>', chunk),
                "reviewed_at": match_text(r'<time datetime="([^"]+)"', chunk),
                "body": text_content(body_match.group(1)),
                "canonical_url": BASE_URL + canonical_match.group(1),
                "source_page": page,
            }
        )
    return reviews


def max_review_page(source: str) -> int:
    pages = [int(value) for value in re.findall(r'/u/starfoxa/reviews\?page=(\d+)', source)]
    return max(pages, default=1)


def store_raw(root: Path, relative: Path, payload: bytes) -> tuple[str, str]:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wb", compresslevel=9) as output:
        output.write(payload)
    return str(relative), hashlib.sha256(payload).hexdigest()


def save_reviews(db: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    db.executemany(
        """INSERT INTO reviews(
               review_id,title,game_url,release_year,rating,play_status,platform,
               reviewed_at,body,canonical_url,source_page,parsed_at
           ) VALUES(
               :review_id,:title,:game_url,:release_year,:rating,:play_status,:platform,
               :reviewed_at,:body,:canonical_url,:source_page,:parsed_at
           ) ON CONFLICT(review_id) DO UPDATE SET
               title=excluded.title,game_url=excluded.game_url,
               release_year=excluded.release_year,rating=excluded.rating,
               play_status=excluded.play_status,platform=excluded.platform,
               reviewed_at=excluded.reviewed_at,body=excluded.body,
               canonical_url=excluded.canonical_url,source_page=excluded.source_page,
               parsed_at=excluded.parsed_at""",
        [dict(row, parsed_at=utc_now()) for row in rows],
    )


def collect(db: sqlite3.Connection, root: Path, limit: int = 0, delay: float = 0.5) -> None:
    profile_payload = request_bytes(PROFILE_URL)
    profile_path, profile_sha = store_raw(root, Path("raw/profile.html.gz"), profile_payload)
    profile = parse_profile(profile_payload.decode("utf-8", "replace"))
    db.execute(
        """INSERT OR REPLACE INTO profile(
               username,profile_url,bio,total_games,raw_path,raw_sha256,fetched_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            profile["username"], profile["profile_url"], profile["bio"],
            profile["total_games"], profile_path, profile_sha, utc_now(),
        ),
    )
    db.commit()

    first_payload = request_bytes(REVIEWS_URL)
    total_pages = max_review_page(first_payload.decode("utf-8", "replace"))
    pending = [
        page
        for page in range(1, total_pages + 1)
        if not db.execute("SELECT 1 FROM pages WHERE page=? AND error IS NULL", (page,)).fetchone()
    ]
    if limit:
        pending = pending[:limit]
    for number, page in enumerate(pending, 1):
        url = REVIEWS_URL if page == 1 else f"{REVIEWS_URL}?page={page}"
        try:
            payload = first_payload if page == 1 else request_bytes(url)
            raw_path, raw_sha = store_raw(root, Path("raw/reviews") / f"page-{page:03d}.html.gz", payload)
            rows = parse_reviews(payload.decode("utf-8", "replace"), page)
            save_reviews(db, rows)
            db.execute(
                """INSERT OR REPLACE INTO pages(
                       page,url,raw_path,raw_sha256,review_count,fetched_at,error
                   ) VALUES(?,?,?,?,?,?,NULL)""",
                (page, url, raw_path, raw_sha, len(rows), utc_now()),
            )
        except Exception as exc:
            db.execute(
                """INSERT OR REPLACE INTO pages(page,url,review_count,fetched_at,error)
                   VALUES(?,?,0,?,?)""",
                (page, url, utc_now(), f"{type(exc).__name__}: {exc}"),
            )
        db.commit()
        print(f"Backloggd pages: {number}/{len(pending)} (page {page})", flush=True)
        time.sleep(delay)


def export_jsonl(db: sqlite3.Connection, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for row in db.execute("SELECT * FROM reviews ORDER BY reviewed_at,review_id"):
            output.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def status(db: sqlite3.Connection) -> dict[str, Any]:
    profile = db.execute("SELECT * FROM profile WHERE username='starfoxa'").fetchone()
    return {
        "profile": dict(profile) if profile else None,
        "pages": db.execute("SELECT COUNT(*) FROM pages WHERE error IS NULL").fetchone()[0],
        "page_errors": db.execute("SELECT COUNT(*) FROM pages WHERE error IS NOT NULL").fetchone()[0],
        "reviews": db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
        "first_review": db.execute("SELECT MIN(reviewed_at) FROM reviews").fetchone()[0],
        "last_review": db.execute("SELECT MAX(reviewed_at) FROM reviews").fetchone()[0],
    }


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "backloggd")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    db = connect(args.data_dir / "backloggd.sqlite3")
    if not args.status:
        collect(db, args.data_dir, args.limit, args.delay)
        export_jsonl(db, args.data_dir / "reviews.jsonl")
    print(json.dumps(status(db), indent=2))
    db.close()


if __name__ == "__main__":
    main()
