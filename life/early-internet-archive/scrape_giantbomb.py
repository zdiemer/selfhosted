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


def extract_class_inner(source: str, tag: str, class_name: str) -> str:
    """Return one class-matched element's inner HTML while respecting nesting."""
    start = re.search(
        rf"(?is)<{tag}\b[^>]*class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>",
        source,
    )
    if not start:
        return ""
    token = re.compile(rf"(?is)<{tag}\b[^>]*>|</{tag}\s*>")
    depth = 1
    for match in token.finditer(source, start.end()):
        if match.group(0).lower().startswith(f"</{tag}"):
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            return source[start.end() : match.start()]
    return ""


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


def parse_review_links(source: str) -> list[dict[str, Any]]:
    """Recover every attributed review route exposed by a profile review index."""
    reviews: dict[str, dict[str, Any]] = {}
    link_pattern = re.compile(
        r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>"
    )
    for match in link_pattern.finditer(source):
        raw_url, raw_label = match.groups()
        url = absolute_url(raw_url)
        parsed = urllib.parse.urlsplit(url)
        review_id = ""
        modern = re.search(r"/user-reviews/2200-(\d+)", parsed.path)
        if modern:
            review_id = modern.group(1)
        else:
            query_id = urllib.parse.parse_qs(parsed.query).get("review_id", [])
            if query_id and query_id[0].isdigit():
                review_id = query_id[0]
        if not review_id:
            continue
        normalized = urllib.parse.urlunsplit(
            ("https", "www.giantbomb.com", parsed.path, parsed.query, "")
        )
        record = reviews.setdefault(
            review_id,
            {
                "review_id": review_id,
                "game_title": parsed.path.strip("/").split("/")[0].replace("-", " ").title(),
                "headline": clean_text(raw_label),
                "rating": None,
                "reviewed_at": "",
                "excerpt": "",
                "urls": [],
            },
        )
        record["urls"].append(normalized)
        label = clean_text(raw_label)
        if len(label) > len(record["headline"]):
            record["headline"] = label
        following = source[match.end() : match.end() + 2500]
        excerpt = re.search(r"(?is)<p\b[^>]*>(.*?)</p>", following)
        if excerpt and len(clean_text(excerpt.group(1))) > len(record["excerpt"]):
            record["excerpt"] = clean_text(excerpt.group(1))
        rating = re.search(r"/star-(\d+)\.png", following, re.I)
        if rating:
            record["rating"] = int(rating.group(1))
        reviewed_at = re.search(
            r"(?is)Reviewed by\s*<a[^>]*>\s*StarFoxA\s*</a>\s*on\s*([^<]+)",
            following,
        )
        if reviewed_at:
            record["reviewed_at"] = parse_date(clean_text(reviewed_at.group(1)))
    for record in reviews.values():
        record["urls"] = sorted(
            set(record["urls"]),
            key=lambda value: ("/2200-" not in value, value),
        )
    return list(reviews.values())


def parse_review_page(
    source: str,
    canonical_url: str,
    review_id: str,
) -> dict[str, Any] | None:
    body = clean_text(extract_class_inner(source, "div", "user-review-body"))
    if len(body) < 100 or "/profile/starfoxa/" not in source.lower():
        return None
    header_match = re.search(
        r'(?is)<h3\b[^>]*class=["\'][^"\']*header-border[^"\']*["\'][^>]*>(.*?)</h3>',
        source,
    )
    header = clean_text(header_match.group(1)) if header_match else ""
    game_match = re.search(r"(?is)starfoxa's\s+(.*?)\s+\([^)]+\)\s+review", header)
    title_match = re.search(
        r'(?is)<h1\b[^>]*>\s*<a\b[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*wiki-title[^"\']*["\'][^>]*>(.*?)</a>',
        source,
    )
    article = extract_class_inner(source, "article", "content-body")
    headline_match = re.search(r"(?is)<h2\b[^>]*>(.*?)</h2>", article)
    page_title_match = re.search(r"(?is)<title>(.*?)</title>", source)
    date_match = re.search(r'<time\b[^>]*datetime=["\']([^"\']+)', source, re.I)
    score_match = re.search(r"Score:\s*<span\b[^>]*class=[\"'][^\"']*score-(\d+)", source, re.I)
    parsed_url = urllib.parse.urlsplit(canonical_url)
    slug = parsed_url.path.strip("/").split("/")[0]
    return {
        "review_id": review_id,
        "game_title": (
            clean_text(game_match.group(1))
            if game_match
            else slug.replace("-", " ").title()
        ),
        "game_url": absolute_url(title_match.group(1)) if title_match else f"{BASE_URL}/{slug}/",
        "headline": (
            clean_text(headline_match.group(1))
            if headline_match
            else clean_text(page_title_match.group(1)) if page_title_match else "Review"
        ),
        "rating": int(score_match.group(1)) * 2 if score_match else None,
        "reviewed_at": date_match.group(1)[:10] if date_match else "",
        "body": body,
        "canonical_url": canonical_url,
    }


def page_title(source: str) -> str:
    match = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", source)
    title = clean_text(match.group(1)) if match else ""
    return re.sub(r"\s+-\s+(?:Giant Bomb|giantbomb\.com)\s*$", "", title, flags=re.I)


def parse_blog_page(
    source: str,
    canonical_url: str,
    blog_id: str,
) -> dict[str, Any] | None:
    body = clean_text(extract_class_inner(source, "div", "blog-copy"))
    byline = re.search(
        r"(?is)<span\b[^>]*class=[\"'][^\"']*\bbyline\b[^\"']*[\"'][^>]*>(.*?)</span>",
        source,
    )
    byline_text = clean_text(byline.group(1)) if byline else ""
    byline_end = byline.end() if byline else 0
    if not byline_text:
        generic_byline = re.search(
            r"(?is)(?:Added\s+by|By)\s*.*?StarFoxA.*?(?:\bon\s+)?"
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z.]*\s+\d{1,2},\s+\d{4})",
            source,
        )
        byline_text = clean_text(generic_byline.group(0)) if generic_byline else ""
        byline_end = generic_byline.end() if generic_byline else 0
    if not body:
        article = extract_class_inner(source, "article", "profile-blog")
        if article:
            article = re.sub(
                r'(?is)<section\b[^>]*class=["\'][^"\']*\bnews-hdr\b[^"\']*["\'][^>]*>.*?</section>',
                "",
                article,
                count=1,
            )
            article = re.sub(
                r'(?is)<section\b[^>]*class=["\'][^"\']*\bprofile-blog-edit\b[^"\']*["\'][^>]*>.*?</section>',
                "",
                article,
                count=1,
            )
            body = clean_text(article)
    if not body and byline_end:
        body = clean_text(extract_class_inner(source[byline_end:], "div", "pl-10"))
    if not body:
        post = extract_class_inner(source, "div", "post")
        subtitle = re.search(
            r'(?is)<div\b[^>]*class=["\'][^"\']*\bblog-sub-title\b[^"\']*["\'][^>]*>.*?</div>',
            post,
        )
        if subtitle:
            post = post[subtitle.end() :]
            post = re.sub(
                r'(?is)^\s*<p\b[^>]*class=["\'][^"\']*\bbold\b[^"\']*["\'][^>]*>.*?</p>',
                "",
                post,
            )
            post = re.split(
                r'(?is)<div\b[^>]*class=["\'][^"\']*\bfl\b[^"\']*["\'][^>]*>\s*<a\b[^>]*class=["\'][^"\']*\bcomment-link\b',
                post,
                maxsplit=1,
            )[0]
            body = clean_text(post)
    if len(body) < 20 or "starfoxa" not in byline_text.lower():
        return None
    date_match = re.search(
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z.]*\s+\d{1,2},\s+\d{4})",
        byline_text,
        re.I,
    )
    return {
        "content_id": f"blog:{blog_id}",
        "kind": "blog",
        "external_id": blog_id,
        "title": page_title(source) or f"Blog {blog_id}",
        "body": body,
        "published_at": parse_date(date_match.group(1)) if date_match else "",
        "canonical_url": canonical_url,
        "metadata": {},
    }


def parse_list_page(
    source: str,
    canonical_url: str,
    list_id: str,
) -> dict[str, Any] | None:
    title = ""
    description = ""
    article = extract_class_inner(source, "article", "content-body")
    if article:
        heading = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", article)
        title = clean_text(heading.group(1)) if heading else ""
        without_heading = re.sub(r"(?is)<h1\b[^>]*>.*?</h1>", "", article, count=1)
        description = clean_text(without_heading)
    if not title:
        legacy = re.search(
            r"(?is)<div\b[^>]*class=[\"'][^\"']*\bvlgray-top\b[^\"']*[\"'][^>]*>"
            r"\s*<span[^>]*>(.*?)</span>\s*</div>\s*"
            r"<div\b[^>]*class=[\"'][^\"']*\bdgray-body\b[^\"']*[\"'][^>]*>(.*?)</div>",
            source,
        )
        if legacy:
            title = clean_text(legacy.group(1))
            description = clean_text(legacy.group(2))
    if not title:
        title = page_title(source)
    if not description:
        description = clean_text(extract_class_inner(source, "div", "list-description"))

    items: dict[str, dict[str, str]] = {}
    for row in re.findall(
        r'(?is)<tr\b[^>]*id=["\']div_list_listitem_\d+["\'][^>]*>(.*?)</tr>',
        source,
    ):
        item = re.search(
            r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*\bbold\b[^"\']*["\'][^>]*>(.*?)</a>',
            row,
        )
        if not item:
            title_cell = extract_class_inner(row, "td", "title")
            item = re.search(
                r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                title_cell,
            )
        if not item:
            continue
        item_title = re.sub(r"^\s*\d+\.\s*", "", clean_text(item.group(2)))
        item_url = absolute_url(item.group(1))
        note = re.search(r"(?is)<p\b[^>]*>(.*?)</p>", row)
        items[item_url] = {
            "title": item_title,
            "url": item_url,
            "note": clean_text(note.group(1)) if note else "",
        }

    modern_list = extract_class_inner(source, "ul", "user-list")
    for item in re.finditer(
        r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>.*?'
        r"<h3\b[^>]*>(.*?)</h3>.*?"
        r'<span\b[^>]*class=["\'][^"\']*\bdeck\b[^"\']*["\'][^>]*>(.*?)</span>.*?</a>',
        modern_list,
    ):
        item_url = absolute_url(item.group(1))
        items[item_url] = {
            "title": clean_text(item.group(2)),
            "url": item_url,
            "note": clean_text(item.group(3)),
        }

    if not title or (not description and not items):
        return None
    lines = [description] if description else []
    if items:
        lines.extend(
            ["List items:"]
            + [
                f"{index}. {item['title']}" + (f" — {item['note']}" if item["note"] else "")
                for index, item in enumerate(items.values(), 1)
            ]
        )
    return {
        "content_id": f"list:{list_id}",
        "kind": "list",
        "external_id": list_id,
        "title": title,
        "body": "\n".join(lines),
        "published_at": "",
        "canonical_url": canonical_url,
        "metadata": {"items": list(items.values()), "item_count": len(items)},
    }


def parse_forum_posts(source: str, canonical_url: str) -> list[dict[str, Any]]:
    before_messages = source.split('id="js-message-', 1)[0]
    headings = [clean_text(value) for value in re.findall(r"(?is)<h1\b[^>]*>(.*?)</h1>", before_messages)]
    topic = headings[-1] if headings else page_title(source).split(" - ")[0]
    posts: list[dict[str, Any]] = []
    for chunk in re.split(r'<div\s+id=["\']js-message-\d+["\'][^>]*>', source, flags=re.I)[1:]:
        author = re.search(
            r'(?is)<a\b[^>]*class=["\'][^"\']*\bmessage-user\b[^"\']*["\'][^>]*>(.*?)</a>',
            chunk,
        )
        author_name = clean_text(author.group(1)) if author else ""
        if author_name.lower() != "starfoxa":
            continue
        permalink = re.search(r'href=["\']([^"\']*#js-message-(\d+))["\']', chunk, re.I)
        if not permalink:
            continue
        message_id = permalink.group(2)
        body = clean_text(extract_class_inner(chunk, "article", "message-body"))
        if not body:
            continue
        sequence = re.search(r">\s*#(\d+)\s*</a>", chunk)
        published = re.search(r'<time\b[^>]*datetime=["\']([^"\']+)', chunk, re.I)
        fragment_url = absolute_url(permalink.group(1))
        posts.append(
            {
                "content_id": f"forum:{message_id}",
                "kind": "forum-post",
                "external_id": message_id,
                "title": f"{topic} — post #{sequence.group(1) if sequence else message_id}",
                "body": body,
                "published_at": published.group(1) if published else "",
                "canonical_url": fragment_url or f"{canonical_url}#js-message-{message_id}",
                "metadata": {"topic": topic, "sequence": int(sequence.group(1)) if sequence else None},
            }
        )
    return posts


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
        CREATE TABLE IF NOT EXISTS content_items (
            content_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            snapshot_count INTEGER NOT NULL,
            raw_paths_json TEXT NOT NULL,
            parsed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_leads (
            review_id TEXT PRIMARY KEY,
            game_title TEXT NOT NULL,
            headline TEXT NOT NULL,
            rating INTEGER,
            reviewed_at TEXT NOT NULL,
            excerpt TEXT NOT NULL,
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
    content_items: dict[str, dict[str, Any]] = {}
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

    review_links: dict[str, dict[str, Any]] = {}
    rows = sweep_db.execute(
        """SELECT timestamp,raw_path FROM captures
           WHERE target_id='giantbomb-starfoxa-reviews' AND raw_path IS NOT NULL
           ORDER BY timestamp"""
    ).fetchall()
    for row in rows:
        with gzip.open(
            sweep_root / "raw" / row["raw_path"],
            "rt",
            encoding="utf-8",
            errors="replace",
        ) as source:
            for parsed in parse_review_links(source.read()):
                record = review_links.setdefault(
                    parsed["review_id"],
                    {
                        "review_id": parsed["review_id"],
                        "game_title": parsed["game_title"],
                        "headline": "",
                        "rating": None,
                        "reviewed_at": "",
                        "excerpt": "",
                        "urls": [],
                        "first_seen": row["timestamp"],
                        "last_seen": row["timestamp"],
                        "raw_paths": [],
                    },
                )
                record["first_seen"] = min(record["first_seen"], row["timestamp"])
                record["last_seen"] = max(record["last_seen"], row["timestamp"])
                record["raw_paths"].append(row["raw_path"])
                record["urls"].extend(parsed["urls"])
                if parsed["game_title"]:
                    record["game_title"] = parsed["game_title"]
                if len(parsed["headline"]) > len(record["headline"]):
                    record["headline"] = parsed["headline"]
                if len(parsed["excerpt"]) > len(record["excerpt"]):
                    record["excerpt"] = parsed["excerpt"]
                if parsed["rating"] is not None:
                    record["rating"] = parsed["rating"]
                if parsed["reviewed_at"]:
                    record["reviewed_at"] = parsed["reviewed_at"]

    for record in review_links.values():
        urls = sorted(
            set(record["urls"]),
            key=lambda value: ("/2200-" not in value, value),
        )
        for index, url in enumerate(urls):
            suffix = "" if index == 0 else f"-alias-{index}"
            sweep_db.execute(
                """INSERT OR REPLACE INTO targets(id,source,kind,url,attribution)
                   VALUES(?,?,?,?,?)""",
                (
                    f"giantbomb-discovered-review-{record['review_id']}{suffix}",
                    "giantbomb",
                    "review",
                    url,
                    "confirmed",
                ),
            )

    db.execute("DELETE FROM review_leads")
    now = utc_now()
    for record in review_links.values():
        db.execute(
            "INSERT INTO review_leads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["review_id"], record["game_title"], record["headline"],
                record["rating"], record["reviewed_at"], record["excerpt"],
                record["urls"][0], record["first_seen"], record["last_seen"],
                len(set(record["raw_paths"])), json.dumps(sorted(set(record["raw_paths"]))), now,
            ),
        )

    rows = sweep_db.execute(
        """SELECT c.timestamp,c.raw_path,t.id target_id,t.url
           FROM captures c JOIN targets t ON t.id=c.target_id
           WHERE c.raw_path IS NOT NULL AND (
               t.id='giantbomb-eternal-darkness-review'
               OR t.id LIKE 'giantbomb-discovered-review-%'
           )
           ORDER BY t.id,c.timestamp"""
    ).fetchall()
    for row in rows:
        id_match = re.search(r"review-(\d+)", row["target_id"])
        review_id = id_match.group(1) if id_match else "24164"
        with gzip.open(
            sweep_root / "raw" / row["raw_path"],
            "rt",
            encoding="utf-8",
            errors="replace",
        ) as source:
            parsed = parse_review_page(source.read(), row["url"], review_id)
        if not parsed:
            continue
        index_record = review_links.get(review_id)
        if index_record:
            parsed["canonical_url"] = index_record["urls"][0]
            parsed["headline"] = index_record["headline"] or parsed["headline"]
            parsed["rating"] = index_record["rating"] or parsed["rating"]
            parsed["reviewed_at"] = index_record["reviewed_at"] or parsed["reviewed_at"]
        record = reviews.get(review_id)
        if record is None:
            reviews[review_id] = dict(
                parsed,
                first_seen=row["timestamp"],
                last_seen=row["timestamp"],
                raw_paths=[row["raw_path"]],
            )
            continue
        record["first_seen"] = min(record["first_seen"], row["timestamp"])
        record["last_seen"] = max(record["last_seen"], row["timestamp"])
        record["raw_paths"].append(row["raw_path"])
        if len(parsed["body"]) > len(record["body"]):
            for key in (
                "game_title",
                "game_url",
                "headline",
                "reviewed_at",
                "body",
                "canonical_url",
            ):
                record[key] = parsed[key]
        if parsed["rating"] is not None:
            record["rating"] = parsed["rating"]

    rows = sweep_db.execute(
        """SELECT c.timestamp,c.original_url,c.raw_path,t.id target_id,t.kind,t.url
           FROM captures c JOIN targets t ON t.id=c.target_id
           WHERE c.raw_path IS NOT NULL
             AND t.source='giantbomb'
             AND t.kind IN ('user-blog','user-list','forum-thread')
           ORDER BY t.id,c.timestamp"""
    ).fetchall()
    for row in rows:
        with gzip.open(
            sweep_root / "raw" / row["raw_path"],
            "rt",
            encoding="utf-8",
            errors="replace",
        ) as source:
            payload = source.read()
        id_match = re.search(r"(?:blog|list)-(\d+)", row["target_id"])
        parsed_items: list[dict[str, Any]] = []
        if row["kind"] == "user-blog" and id_match:
            parsed = parse_blog_page(payload, row["url"], id_match.group(1))
            if parsed:
                parsed_items.append(parsed)
        elif row["kind"] == "user-list" and id_match:
            parsed = parse_list_page(payload, row["url"], id_match.group(1))
            if parsed:
                parsed_items.append(parsed)
            for raw_url in re.findall(
                r'(?is)<a\b[^>]*href=["\']([^"\']*[?&]page=(\d+)[^"\']*)["\']',
                payload,
            ):
                page_url, page_number = raw_url
                page_url = absolute_url(page_url)
                page_url = urllib.parse.urlunsplit(
                    urllib.parse.urlsplit(page_url)._replace(fragment="")
                )
                sweep_db.execute(
                    """INSERT OR REPLACE INTO targets(id,source,kind,url,attribution)
                       VALUES(?,?,?,?,?)""",
                    (
                        f"giantbomb-discovered-list-{id_match.group(1)}-page-{page_number}",
                        "giantbomb", "user-list", page_url, "confirmed",
                    ),
                )
        elif row["kind"] == "forum-thread":
            parsed_items.extend(parse_forum_posts(payload, row["url"]))

        for parsed in parsed_items:
            record = content_items.get(parsed["content_id"])
            if record is None:
                content_items[parsed["content_id"]] = dict(
                    parsed,
                    first_seen=row["timestamp"],
                    last_seen=row["timestamp"],
                    raw_paths=[row["raw_path"]],
                )
                continue
            record["first_seen"] = min(record["first_seen"], row["timestamp"])
            record["last_seen"] = max(record["last_seen"], row["timestamp"])
            record["raw_paths"].append(row["raw_path"])
            if len(parsed["title"]) > len(record["title"]):
                record["title"] = parsed["title"]
            if parsed["published_at"] and not record["published_at"]:
                record["published_at"] = parsed["published_at"]
            if "/lists/" in parsed["canonical_url"]:
                record["canonical_url"] = parsed["canonical_url"]
            if parsed["kind"] == "list":
                merged = {
                    item["url"]: item
                    for item in record["metadata"].get("items", [])
                }
                merged.update(
                    {item["url"]: item for item in parsed["metadata"].get("items", [])}
                )
                items = list(merged.values())
                record["metadata"] = {"items": items, "item_count": len(items)}
                description = record["body"].split("\nList items:\n", 1)[0]
                candidate_description = parsed["body"].split("\nList items:\n", 1)[0]
                if len(candidate_description) > len(description):
                    description = candidate_description
                record["body"] = "\n".join(
                    ([description] if description else [])
                    + (["List items:"] if items else [])
                    + [
                        f"{index}. {item['title']}"
                        + (f" — {item['note']}" if item["note"] else "")
                        for index, item in enumerate(items, 1)
                    ]
                )
            elif len(parsed["body"]) > len(record["body"]):
                record["body"] = parsed["body"]
                record["metadata"] = parsed["metadata"]

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
    db.execute("DELETE FROM content_items")
    for record in content_items.values():
        db.execute(
            """INSERT INTO content_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["content_id"], record["kind"], record["external_id"],
                record["title"], record["body"], record["published_at"],
                record["canonical_url"], json.dumps(record["metadata"], ensure_ascii=False),
                record["first_seen"], record["last_seen"],
                len(set(record["raw_paths"])),
                json.dumps(sorted(set(record["raw_paths"]))), now,
            ),
        )
    db.commit()
    sweep_db.commit()


def export_jsonl(db: sqlite3.Connection, root: Path) -> None:
    for table, filename, order in (
        ("artifacts", "artifacts.jsonl", "kind,legacy_id"),
        ("reviews", "reviews.jsonl", "reviewed_at,review_id"),
        ("content_items", "content_items.jsonl", "kind,published_at,external_id"),
        ("review_leads", "review_leads.jsonl", "reviewed_at,review_id"),
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
        "review_leads": db.execute("SELECT COUNT(*) FROM review_leads").fetchone()[0],
        "content_items": db.execute("SELECT COUNT(*) FROM content_items").fetchone()[0],
        "content_by_kind": {
            row["kind"]: row["count"]
            for row in db.execute(
                "SELECT kind,COUNT(*) count FROM content_items GROUP BY kind ORDER BY kind"
            )
        },
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
