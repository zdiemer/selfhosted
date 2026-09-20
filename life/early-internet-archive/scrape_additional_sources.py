#!/usr/bin/env python3
"""Normalize attributed material preserved by the breadth-first source sweep."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_BY_SOURCE = {
    "backloggd": "https://backloggd.com",
    "chipmusic": "https://chipmusic.org",
    "ds_fanboy": "https://www.dsfanboy.com",
    "gog": "https://www.gog.com",
    "gtfoutsider": "http://gtfoutsider.com",
    "itch_io": "https://itch.io",
    "math_stackexchange": "https://math.stackexchange.com",
    "spriters_resource": "https://www.spriters-resource.com",
    "steam": "https://steamcommunity.com",
    "supercheats": "https://www.supercheats.com",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(?:script|style)\b.*?</(?:script|style)>", "", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(?:p|div|li|blockquote|h\d|tr)\s*>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment).replace("\r", "").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in fragment.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_class_inner(source: str, tag: str, class_name: str) -> str:
    start = re.search(
        rf"(?is)<{tag}\b[^>]*class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>",
        source,
    )
    if not start:
        return ""
    token = re.compile(rf"(?is)<{tag}\b[^>]*>|</{tag}\s*>")
    depth = 1
    for match in token.finditer(source, start.end()):
        depth += -1 if match.group(0).lower().startswith(f"</{tag}") else 1
        if depth == 0:
            return source[start.end() : match.start()]
    return ""


def page_title(source: str) -> str:
    match = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", source)
    return clean_text(match.group(1)) if match else ""


def absolute_url(source_name: str, value: str) -> str:
    return urllib.parse.urljoin(BASE_BY_SOURCE[source_name] + "/", html.unescape(value))


def parsed_date(value: str) -> str:
    value = clean_text(value).strip().rstrip(".")
    value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value, flags=re.I)
    for pattern in (
        "%b %d, %Y %I:%M %p",
        "%b %d, %Y @ %I:%M%p",
        "%B %d, %Y",
        "%b %d, %Y",
        "%m-%d-%Y @ %I:%M%p",
        "%b %d %Y",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(value, pattern).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def record(
    source: str,
    kind: str,
    external_id: str,
    title: str,
    body: str,
    canonical_url: str,
    *,
    author: str = "StarFoxA",
    published_at: str = "",
    section: str = "",
    completeness: str = "full",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "content_id": f"{source}:{external_id}",
        "source": source,
        "kind": kind,
        "external_id": external_id,
        "title": title.strip(),
        "body": body.strip(),
        "author": author,
        "published_at": published_at,
        "canonical_url": canonical_url,
        "section": section or source,
        "completeness": completeness,
        "tags": tags or [],
        "metadata": metadata or {},
    }


def parse_chipmusic(source: str) -> list[dict[str, Any]]:
    title = re.sub(r"\s*\(Page \d+\).*", "", page_title(source))
    posts: list[dict[str, Any]] = []
    chunks = re.split(r'<div\s+id=["\']p(\d+)["\']\s+class=["\']posthead["\']>', source)
    for index in range(1, len(chunks), 2):
        post_id, chunk = chunks[index], chunks[index + 1]
        author = re.search(r'(?is)<div class=["\']username["\']>.*?>([^<]+)</a>', chunk)
        if not author or clean_text(author.group(1)).lower() != "starfoxa":
            continue
        body = clean_text(extract_class_inner(chunk, "div", "post-entry"))
        permalink = re.search(r'(?is)<a\b[^>]*class=["\']permalink["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', chunk)
        posts.append(
            record(
                "chipmusic", "forum-post", post_id, title, body,
                html.unescape(permalink.group(1)) if permalink else "",
                published_at=parsed_date(permalink.group(2)) if permalink else "",
                section="General Discussion",
            )
        )
    return posts


def parse_ds_fanboy(source: str, canonical_url: str) -> list[dict[str, Any]]:
    title = re.sub(r"\s+-\s+DS Fanboy$", "", page_title(source), flags=re.I)
    posts: list[dict[str, Any]] = []
    chunks = re.split(r'<div\b[^>]*class=["\']commentlinks["\'][^>]*>', source)
    for chunk in chunks[1:]:
        comment_id = re.search(r'id=["\']c(\d+)["\']', chunk)
        author = re.search(r'(?is)<h4\b[^>]*class=["\']authorname["\'][^>]*>(.*?)</h4>', chunk)
        if not comment_id or not author or "starfoxa" not in clean_text(author.group(1)).lower():
            continue
        date = re.search(r"(?is)<em>(.*?)</em>", chunk)
        body = re.search(r"(?is)</div>\s*<p>(.*?)</p>", chunk)
        if not body:
            continue
        posts.append(
            record(
                "ds_fanboy", "comment", comment_id.group(1), title,
                clean_text(body.group(1)), f"{canonical_url}#c{comment_id.group(1)}",
                published_at=parsed_date(date.group(1)) if date else "",
                section="DS Fanboy",
            )
        )
    return posts


def parse_gog(source: str) -> list[dict[str, Any]]:
    title = re.sub(r", page \d+\s+-\s+Forum\s+-\s+GOG\.com$", "", page_title(source), flags=re.I)
    posts: list[dict[str, Any]] = []
    chunks = re.split(r'<div id=["\']p_s_(\d+)["\']', source)
    for index in range(1, len(chunks), 2):
        sequence, chunk = chunks[index], chunks[index + 1]
        if 'gog-user="809111026997"' not in chunk:
            continue
        body = re.search(r'(?is)<div class=["\']post_text_c["\']>(.*?)</div></div>', chunk)
        date = re.search(r"Posted\s+([^<]+)", chunk)
        permalink = re.search(r'(?is)<div class=["\']post_nr["\']>\s*<a[^>]*href=["\']([^"\']+)', chunk)
        if not body:
            continue
        posts.append(
            record(
                "gog", "forum-post", sequence, f"{title} — post #{sequence}",
                clean_text(body.group(1)), absolute_url("gog", permalink.group(1)) if permalink else "",
                published_at=parsed_date(date.group(1)) if date else "",
                section="GOG Forums",
            )
        )
    return posts


def parse_itch(source: str) -> list[dict[str, Any]]:
    title = re.sub(r"^Comments .*? - ", "", page_title(source))
    posts: list[dict[str, Any]] = []
    chunks = re.split(r'<div\b[^>]*class=["\'][^"\']*\bcommunity_post\b[^"\']*["\'][^>]*id=["\']post-(\d+)["\'][^>]*>', source)
    for index in range(1, len(chunks), 2):
        post_id, chunk = chunks[index], chunks[index + 1]
        author = re.search(r'(?is)<span class=["\']post_author["\']>(.*?)</span>', chunk)
        if not author or clean_text(author.group(1)).lower() != "starfoxa":
            continue
        body = clean_text(extract_class_inner(chunk, "div", "post_body"))
        date_tag = re.search(r'<span\b(?=[^>]*class=["\']post_date["\'])[^>]*>', chunk)
        date = re.search(r'title=["\']([^"\']+)', date_tag.group(0)) if date_tag else None
        posts.append(
            record(
                "itch_io", "comment", post_id, f"{title} — comment", body,
                f"https://itch.io/post/{post_id}",
                published_at=parsed_date(date.group(1)) if date else "",
                section="itch.io",
            )
        )
    return posts


def parse_math(source: str, canonical_url: str, question_id: str) -> list[dict[str, Any]]:
    title = re.sub(r"\s+-\s+Mathematics Stack Exchange$", "", page_title(source))
    question = source.split('<div class="question"', 1)[-1]
    body = clean_text(extract_class_inner(question, "div", "post-text"))
    if not body or f"/users/175458/starfoxa" not in question.split('id="answers"', 1)[0].lower():
        return []
    date = re.search(r'asked\s*<span\b[^>]*title=["\']([^"\']+)', question, re.I)
    tags = [clean_text(value) for value in re.findall(r'(?is)<a\b[^>]*class=["\']post-tag["\'][^>]*>(.*?)</a>', question.split('id="answers"', 1)[0])]
    comments: list[str] = []
    for chunk in re.split(r'<tr\b[^>]*id=["\']comment-\d+["\'][^>]*>', question)[1:]:
        chunk = chunk.split("</tr>", 1)[0]
        if "/users/175458/starfoxa" not in chunk.lower():
            continue
        text = clean_text(extract_class_inner(chunk, "span", "comment-copy"))
        if text:
            comments.append(text)
    if comments:
        body += "\n\nFollow-up comments:\n" + "\n".join(f"• {value}" for value in comments)
    return [
        record(
            "math_stackexchange", "question", question_id, title, body, canonical_url,
            author="starfoxa", published_at=parsed_date(date.group(1)) if date else "",
            section="Mathematics Stack Exchange", tags=tags,
            metadata={"follow_up_comment_count": len(comments)},
        )
    ]


def parse_steam_reviews(source: str) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for chunk in source.split('class="review_box"')[1:]:
        app = re.search(r"https://steamcommunity\.com/app/(\d+)", chunk)
        body = clean_text(extract_class_inner(chunk, "div", "content"))
        if not app or not body:
            continue
        app_id = app.group(1)
        recommendation = clean_text(extract_class_inner(chunk, "div", "title"))
        hours = clean_text(extract_class_inner(chunk, "div", "hours"))
        posted = clean_text(extract_class_inner(chunk, "div", "posted"))
        posted = re.sub(r"^Posted\s+", "", posted, flags=re.I)
        reviews.append(
            record(
                "steam", "review", app_id, f"Steam app {app_id}: {recommendation}", body,
                f"https://steamcommunity.com/id/starfoxa/recommended/{app_id}/",
                published_at=parsed_date(posted), section="Steam",
                tags=[recommendation] if recommendation else [],
                metadata={"app_id": app_id, "recommendation": recommendation, "hours": hours},
            )
        )
    return reviews


def steam_screenshots(source: str) -> list[dict[str, Any]]:
    screenshots: list[dict[str, Any]] = []
    starts = list(re.finditer(r'<a\b[^>]*data-publishedfileid=["\'](\d+)["\'][^>]*>', source, re.I))
    for index, start in enumerate(starts):
        chunk = source[start.start() : starts[index + 1].start() if index + 1 < len(starts) else start.start() + 5000]
        screenshot_id = start.group(1)
        app = re.search(r'data-appid=["\'](\d+)["\']', start.group(0), re.I)
        href = re.search(r'href=["\']([^"\']+)', start.group(0), re.I)
        image_url = re.search(r"background-image:\s*url\(['\"]?([^'\")]+)", chunk, re.I)
        app_id = app.group(1) if app else ""
        screenshots.append(
            record(
                "steam", "screenshot", screenshot_id,
                f"Steam screenshot {screenshot_id}",
                f"Screenshot from Steam app {app_id}." if app_id else "Steam screenshot.",
                html.unescape(href.group(1)) if href else f"https://steamcommunity.com/sharedfiles/filedetails/?id={screenshot_id}",
                section="Steam", completeness="metadata-only",
                metadata={
                    "app_id": app_id,
                    "image_url": html.unescape(image_url.group(1)) if image_url else "",
                },
            )
        )
    return screenshots


def parse_steam_review_detail(source: str, app_id: str, canonical_url: str) -> list[dict[str, Any]]:
    title = page_title(source)
    game = re.sub(r"^Steam Community\s*::\s*StarFoxA\s*::\s*Review for\s*", "", title, flags=re.I)
    if not game or game == title:
        return []
    recommendation = clean_text(extract_class_inner(source, "div", "title"))
    body = clean_text(extract_class_inner(source, "div", "content"))
    if not body:
        description = re.search(r'(?is)<meta\b[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)', source)
        body = clean_text(description.group(1)) if description else ""
    return [record(
        "steam", "review", app_id,
        f"{game}: {recommendation}" if recommendation else game,
        body, canonical_url, section="Steam",
        tags=[recommendation] if recommendation else [],
        metadata={"app_id": app_id, "game_title": game, "recommendation": recommendation},
    )] if body else []


def parse_steam_screenshot_detail(source: str, screenshot_id: str, canonical_url: str) -> list[dict[str, Any]]:
    game = clean_text(extract_class_inner(source, "div", "apphub_AppName"))
    if not game:
        return []
    image = re.search(
        r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>\s*<img\b[^>]*id=["\']ActualMedia["\']',
        source,
    )
    stats = [clean_text(value) for value in re.findall(
        r'(?is)<div\b[^>]*class=["\'][^"\']*detailsStatRight[^"\']*["\'][^>]*>(.*?)</div>',
        source,
    )]
    published = next((parsed_date(value) for value in stats if re.search(r"\b\d{4}\b.*@", value)), "")
    image_url = html.unescape(image.group(1)) if image else ""
    return [record(
        "steam", "screenshot", screenshot_id, f"{game} — screenshot",
        f"Screenshot from {game}.", canonical_url, published_at=published,
        section="Steam", completeness="full" if image_url else "metadata-only",
        tags=[game], metadata={"game_title": game, "image_url": image_url, "details": stats},
    )]


def parse_supercheats(source: str, canonical_url: str) -> list[dict[str, Any]]:
    marker = re.search(r'(?is)<a\b[^>]*href=["\']/members/StarFoxA["\'][^>]*>StarFoxA</a>\s*posted:', source)
    if not marker:
        return []
    following = source[marker.end() :]
    header = re.search(r'(?is)<i>([^<]*ID#(\d+))</i>.*?<strong>(.*?)</strong>', following)
    if not header:
        return []
    body_match = re.search(r"(?is)<div\b[^>]*id=[\"']sub910[\"'][^>]*>(.*?)</div>", following)
    body = clean_text(body_match.group(1)) if body_match else ""
    date = header.group(1).split(", ID#", 1)[0]
    return [
        record(
            "supercheats", "code-submission", header.group(2), clean_text(header.group(3)),
            body, canonical_url, published_at=parsed_date(date),
            section="SuperCheats", tags=["Pokemon Diamond", "Action Replay"],
        )
    ]


def parse_spriters(source: str, canonical_url: str) -> list[dict[str, Any]]:
    stats = re.search(r'(?is)<table\b[^>]*id=["\']stats["\'][^>]*>(.*?)</table>', source)
    if not stats or "StarFoxA" not in stats.group(1):
        return []
    values: dict[str, str] = {}
    for key, value in re.findall(r"(?is)<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", stats.group(1)):
        values[clean_text(key)] = clean_text(value)
    body = "\n".join(f"{key}: {value}" for key, value in values.items())
    return [
        record(
            "spriters_resource", "sprite-sheet", "13564",
            f"{values.get('Game', 'Uninvited')} — {values.get('Name', 'Scenes')}",
            body, canonical_url, section="The Spriters Resource",
            tags=[values.get("Category", ""), values.get("Game", "")], metadata=values,
        )
    ]


def parse_wikisider(source: str, canonical_url: str) -> list[dict[str, Any]]:
    content = re.search(r"(?is)<!-- start content -->(.*?)<!-- Saved in parser cache", source)
    if not content:
        return []
    return [
        record(
            "gtfoutsider", "third-party-context", "starfoxa-wikisider",
            "STARFOXA — WikiSider profile", clean_text(content.group(1)), canonical_url,
            author="WikiSider contributors", section="WikiSider",
            completeness="full", tags=["third-party context"],
            metadata={"attribution_scope": "third-party context; not authored by StarFoxA"},
        )
    ]


def parse_backloggd_lists(source: str) -> list[dict[str, Any]]:
    lists: list[dict[str, Any]] = []
    pattern = re.compile(
        r'(?is)<h2\b[^>]*class=["\'][^"\']*\blist-display-title\b[^"\']*["\'][^>]*>'
        r'\s*<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>.*?'
        r'<p\b[^>]*class=["\'][^"\']*\bentries-count\b[^"\']*["\'][^>]*>(.*?)</p>'
    )
    for match in pattern.finditer(source):
        url = absolute_url("backloggd", match.group(1))
        slug = urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]
        count = clean_text(match.group(3))
        lists.append(
            record(
                "backloggd", "list", f"list:{slug}", clean_text(match.group(2)),
                count, url, author="starfoxa", section="Backloggd",
                completeness="metadata-only", metadata={"entry_count": count},
            )
        )
    return lists


def local_project_records(repo_root: Path) -> list[dict[str, Any]]:
    fsu = repo_root / "web" / "fsu" / "projects"
    records: list[dict[str, Any]] = []
    definitions = (
        ("breakout", "Breakout", "Breakout", "2014-11-18T09:45:43-05:00", "https://github.com/zdiemer/Breakout"),
        ("my-data-structure", "MyDS", "my-data-structure", "2015-01-26T19:52:45-05:00", "https://github.com/zdiemer/my-data-structure"),
        ("cloysta", "Cloysta", "Cloysta", "2015-08-28T12:38:04-04:00", "https://github.com/zdiemer/Cloysta"),
        ("pybank", "pybank", "pybank", "2016-04-06T17:21:02-04:00", "https://github.com/zdiemer/pybank"),
    )
    fallback = {
        "my-data-structure": "C++ implementations of a hash table and related data structures for Florida State University coursework.",
    }
    for external_id, title, directory, published, url in definitions:
        readme = fsu / directory / "README.md"
        body = readme.read_text(encoding="utf-8", errors="replace") if readme.exists() else fallback.get(external_id, "")
        records.append(
            record(
                "fsu_coursework", "software-project", external_id, title, body, url,
                author="Zach Diemer", published_at=published, section="FSU Coursework",
                tags=["software", "coursework"],
            )
        )
    site = repo_root / "web" / "old-diemer-codes" / "site" / "src" / "Site.jsx"
    if site.exists():
        source = site.read_text(encoding="utf-8", errors="replace")
        paragraphs = [clean_text(value) for value in re.findall(r"(?is)<p>(.*?)</p>", source)]
        body = "\n\n".join(value for value in paragraphs if value)[:12000]
        records.append(
            record(
                "personal_site", "website", "diemer-codes-2018", "diemer.codes (2018)", body,
                "https://github.com/zdiemer/diemer.codes", author="Zach Diemer",
                published_at="2018-03-03T18:38:30-05:00", section="Personal Site",
                tags=["React", "portfolio"],
            )
        )
    return records


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items (
            content_id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
            external_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            author TEXT NOT NULL, published_at TEXT NOT NULL, canonical_url TEXT NOT NULL,
            section TEXT NOT NULL, completeness TEXT NOT NULL, tags_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            snapshot_count INTEGER NOT NULL, raw_paths_json TEXT NOT NULL, parsed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY, source TEXT NOT NULL, original_url TEXT NOT NULL,
            local_path TEXT NOT NULL, mime_type TEXT NOT NULL, captured_at TEXT NOT NULL,
            sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL, item_ids_json TEXT NOT NULL
        );
        """
    )
    return db


def merge_item(items: dict[str, dict[str, Any]], parsed: dict[str, Any], capture: sqlite3.Row | None) -> None:
    timestamp = capture["timestamp"] if capture else parsed.get("published_at", "")
    raw_path = capture["raw_path"] if capture else ""
    current = items.get(parsed["content_id"])
    if current is None:
        items[parsed["content_id"]] = dict(
            parsed, first_seen=timestamp, last_seen=timestamp,
            raw_paths=[raw_path] if raw_path else [],
        )
        return
    current["first_seen"] = min(value for value in (current["first_seen"], timestamp) if value)
    current["last_seen"] = max(current["last_seen"], timestamp)
    if raw_path:
        current["raw_paths"].append(raw_path)
    merged_metadata = dict(current["metadata"])
    if current["completeness"] != "full" or parsed["completeness"] == "full":
        merged_metadata.update({key: value for key, value in parsed["metadata"].items() if value})
    else:
        for key, value in parsed["metadata"].items():
            if value and not merged_metadata.get(key):
                merged_metadata[key] = value
    current["metadata"] = merged_metadata
    if parsed["published_at"] and not current["published_at"]:
        current["published_at"] = parsed["published_at"]
    if parsed["completeness"] == "full" and current["completeness"] != "full":
        current["completeness"] = "full"
        current["canonical_url"] = parsed["canonical_url"]
    if not parsed["title"].startswith("Steam app ") and not parsed["title"].startswith("Steam screenshot "):
        current["title"] = parsed["title"]
    if len(parsed["body"]) > len(current["body"]):
        current["body"] = parsed["body"]
        if current["completeness"] != "full" or parsed["completeness"] == "full":
            for key in ("published_at", "canonical_url", "completeness"):
                current[key] = parsed[key]


def discover_dynamic_targets(sweep_db: sqlite3.Connection, parsed: dict[str, Any]) -> None:
    if parsed["source"] == "backloggd" and parsed["kind"] == "list":
        slug = parsed["external_id"].split(":", 1)[-1]
        sweep_db.execute(
            "INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?)",
            (f"backloggd-starfoxa-list-{slug}", "backloggd", "user-list", parsed["canonical_url"], "confirmed"),
        )
    if parsed["source"] == "steam" and parsed["kind"] == "review":
        sweep_db.execute(
            "INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?)",
            (f"steam-starfoxa-review-{parsed['external_id']}", "steam", "review", parsed["canonical_url"], "confirmed"),
        )
    if parsed["source"] == "steam" and parsed["kind"] == "screenshot":
        screenshot_id = parsed["external_id"]
        sweep_db.execute(
            "INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?)",
            (f"steam-starfoxa-screenshot-{screenshot_id}", "steam", "screenshot", parsed["canonical_url"], "confirmed"),
        )
        image_url = parsed["metadata"].get("image_url")
        if image_url:
            sweep_db.execute(
                "INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?)",
                (f"steam-starfoxa-screenshot-image-{screenshot_id}", "steam", "original-image", image_url, "confirmed"),
            )


def build(data_dir: Path, sweep_dir: Path, repo_root: Path) -> dict[str, Any]:
    db = connect(data_dir / "additional.sqlite3")
    sweep_db = sqlite3.connect(sweep_dir / "source_sweep.sqlite3")
    sweep_db.row_factory = sqlite3.Row
    items: dict[str, dict[str, Any]] = {}
    captures = sweep_db.execute(
        """SELECT c.*,t.source,t.kind,t.url target_url
           FROM captures c JOIN targets t ON t.id=c.target_id
           WHERE c.raw_path IS NOT NULL ORDER BY t.source,t.id,c.timestamp"""
    ).fetchall()
    for capture in captures:
        raw_file = sweep_dir / "raw" / capture["raw_path"]
        if capture["mimetype"] and capture["mimetype"].startswith("image/"):
            continue
        with gzip.open(raw_file, "rt", encoding="utf-8", errors="replace") as payload:
            source = payload.read()
        parsed: list[dict[str, Any]] = []
        target_id = capture["target_id"]
        if target_id == "chipmusic-vocal-chiptunes-thread":
            parsed = parse_chipmusic(source)
        elif target_id == "ds-fanboy-homebrew-comment":
            parsed = parse_ds_fanboy(source, capture["target_url"])
        elif capture["source"] == "gog" and capture["kind"] == "forum-thread":
            parsed = parse_gog(source)
        elif target_id == "itch-bittersweet-birthday-comment":
            parsed = parse_itch(source)
        elif target_id.startswith("math-stackexchange-question-"):
            parsed = parse_math(source, capture["target_url"], target_id.rsplit("-", 1)[-1])
        elif target_id == "steam-starfoxa-reviews":
            parsed = parse_steam_reviews(source)
        elif target_id == "steam-starfoxa-screenshots":
            parsed = steam_screenshots(source)
        elif target_id.startswith("steam-starfoxa-review-"):
            parsed = parse_steam_review_detail(source, target_id.rsplit("-", 1)[-1], capture["target_url"])
        elif target_id.startswith("steam-starfoxa-screenshot-") and "-image-" not in target_id:
            parsed = parse_steam_screenshot_detail(source, target_id.rsplit("-", 1)[-1], capture["target_url"])
        elif target_id == "supercheats-secret-id-modifier":
            parsed = parse_supercheats(source, capture["target_url"])
        elif target_id == "spriters-resource-uninvited-scenes":
            parsed = parse_spriters(source, capture["target_url"])
        elif target_id == "gtfoutsider-starfoxa-wiki":
            parsed = parse_wikisider(source, capture["target_url"])
        elif target_id == "backloggd-starfoxa-lists":
            parsed = parse_backloggd_lists(source)
        for value in parsed:
            discover_dynamic_targets(sweep_db, value)
            merge_item(items, value, capture)

    for value in local_project_records(repo_root):
        merge_item(items, value, None)

    now = utc_now()
    db.execute("DELETE FROM items")
    for value in items.values():
        db.execute(
            "INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                value["content_id"], value["source"], value["kind"], value["external_id"],
                value["title"], value["body"], value["author"], value["published_at"],
                value["canonical_url"], value["section"], value["completeness"],
                json.dumps(value["tags"], ensure_ascii=False),
                json.dumps(value["metadata"], ensure_ascii=False), value["first_seen"],
                value["last_seen"], len(set(value["raw_paths"])),
                json.dumps(sorted(set(value["raw_paths"]))), now,
            ),
        )

    media_root = data_dir / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    db.execute("DELETE FROM assets")
    asset_specs = [("spriters-resource-uninvited-scenes-png", "spriters_resource", "13564", "spriters_resource/13564.png")]
    asset_specs.extend(
        (
            f"steam-starfoxa-screenshot-image-{row['external_id']}", "steam",
            row["external_id"], f"steam/{row['external_id']}.jpg",
        )
        for row in items.values() if row["source"] == "steam" and row["kind"] == "screenshot"
    )
    for target_id, source_name, external_id, local_name in asset_specs:
        capture = sweep_db.execute(
            """SELECT * FROM captures WHERE target_id=? AND raw_path IS NOT NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (target_id,),
        ).fetchone()
        if not capture:
            continue
        with gzip.open(sweep_dir / "raw" / capture["raw_path"], "rb") as payload:
            content = payload.read()
        target = media_root / local_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        target_url = sweep_db.execute("SELECT url FROM targets WHERE id=?", (target_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"{source_name}:{external_id}", source_name, target_url,
                f"additional/media/{local_name}", capture["mimetype"] or "application/octet-stream",
                capture["timestamp"], digest, len(content),
                json.dumps([f"additional:{source_name}:{external_id}"]),
            ),
        )
    db.commit()
    sweep_db.commit()
    counts = {row[0]: row[1] for row in db.execute("SELECT source,COUNT(*) FROM items GROUP BY source ORDER BY source")}
    result = {"items": sum(counts.values()), "by_source": counts, "assets": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]}
    db.close()
    sweep_db.close()
    return result


def export_jsonl(db_path: Path, output: Path) -> None:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    with output.open("w", encoding="utf-8") as stream:
        for row in db.execute("SELECT * FROM items ORDER BY source,published_at,external_id"):
            stream.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    db.close()


def main() -> None:
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=project / "data" / "additional")
    parser.add_argument("--sweep-dir", type=Path, default=project / "data" / "source_sweep")
    args = parser.parse_args()
    result = build(args.data_dir, args.sweep_dir, project.parents[1])
    export_jsonl(args.data_dir / "additional.sqlite3", args.data_dir / "items.jsonl")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
