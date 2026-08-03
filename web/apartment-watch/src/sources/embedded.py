"""Helpers for the three sites that are single-page apps behind a bot wall.

Zumper, Apartments.com, and the Zillow family all render from a JSON blob that
ships inside the HTML — `__NEXT_DATA__`, `__INITIAL_STATE__`, an Apollo cache,
or schema.org JSON-LD. Reading that blob is much more durable than CSS
selectors: these sites reskin constantly, but the data they hand their own
frontend keeps the same field names for years.

So the strategy everywhere below is: find every JSON blob in the page, walk it
for dicts that look like a rental listing, and map those. A redesign that
renames a CSS class doesn't touch it; only a rename of the underlying API
fields does.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]+type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', re.S | re.I
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I
)
# `window.__INITIAL_STATE__ = {...};` and friends. Non-greedy to the line end
# won't work (the blob has newlines), so balance braces instead.
_STATE_ASSIGN_RE = re.compile(
    r'(?:window\.)?(?:__INITIAL_STATE__|__APOLLO_STATE__|__PRELOADED_STATE__|__NUXT__)\s*=\s*'
)


def _balanced(text: str, start: int) -> str | None:
    """Slice the balanced {...} beginning at `start`, ignoring braces in strings."""
    if start >= len(text) or text[start] != "{":
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def json_blobs(html: str) -> Iterator[Any]:
    """Every JSON object embedded in the page that we can parse."""
    if not html:
        return
    for pattern in (_NEXT_DATA_RE, _SCRIPT_JSON_RE):
        for raw in pattern.findall(html):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue
    for m in _STATE_ASSIGN_RE.finditer(html):
        blob = _balanced(html, m.end())
        if not blob:
            continue
        try:
            yield json.loads(blob)
        except json.JSONDecodeError:
            continue


def walk(obj: Any) -> Iterator[dict]:
    """Every dict anywhere in a nested JSON structure."""
    stack = [obj]
    seen = 0
    while stack:
        cur = stack.pop()
        seen += 1
        # Guard against a pathological blob eating the run.
        if seen > 400_000:
            logger.debug("walk: bailing out after %d nodes", seen)
            return
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def first(d: dict, *keys, default=None):
    """First present, non-empty value among `keys` (case-insensitive)."""
    lowered = {k.lower(): v for k, v in d.items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val not in (None, "", [], {}):
            return val
    return default


def as_int(value) -> int | None:
    """Coerce the many shapes these APIs use for a price into an int."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("min", "minimum", "amount", "value", "low", "price"):
            if key in value:
                return as_int(value[key])
        return None
    if isinstance(value, (list, tuple)):
        vals = [as_int(v) for v in value]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None
    if isinstance(value, str):
        m = re.search(r"([0-9][0-9,]*)", value.replace("$", ""))
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def as_bedrooms(value) -> int | None:
    """Bedrooms, with every studio spelling normalized to 0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, tuple)):
        vals = [as_bedrooms(v) for v in value]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None
    if isinstance(value, dict):
        return as_bedrooms(first(value, "min", "minimum", "beds", "bedrooms", "value"))
    if isinstance(value, str):
        text = value.lower()
        if "studio" in text or text.strip() in ("0", "0 bd", "0 br"):
            return 0
        m = re.search(r"([0-9]+)", text)
        return int(m.group(1)) if m else None
    return None


def amenity_text(d: dict) -> str:
    """Flatten a listing dict's amenity-ish fields into one searchable string.

    Each site nests amenities differently and renames the containers regularly,
    so rather than target one path we collect anything that reads like a
    description or feature list and let the shared regex parsers work on it.
    """
    keys = (
        "amenities", "amenityList", "amenitySummary", "features", "featureList",
        "description", "detailedDescription", "summary", "highlights",
        "propertyAmenities", "unitAmenities", "communityAmenities",
        "laundry", "parking", "parkingTypes", "utilities", "attributes",
    )
    chunks: list[str] = []

    def flatten(value, depth=0):
        if depth > 4 or value is None:
            return
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, (int, float, bool)):
            return
        elif isinstance(value, dict):
            for v in value.values():
                flatten(v, depth + 1)
        elif isinstance(value, (list, tuple)):
            for v in value:
                flatten(v, depth + 1)

    lowered = {k.lower(): v for k, v in d.items()}
    for key in keys:
        if key.lower() in lowered:
            flatten(lowered[key.lower()])
    return " ; ".join(chunks)[:20_000]


def coords(d: dict) -> tuple[float | None, float | None]:
    lat = first(d, "latitude", "lat")
    lon = first(d, "longitude", "lng", "lon")
    if lat is None or lon is None:
        geo = first(d, "latLong", "latLng", "location", "geo", "coordinate", "coordinates")
        if isinstance(geo, dict):
            lat = first(geo, "latitude", "lat")
            lon = first(geo, "longitude", "lng", "lon")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    # SF, loosely. Anything else is a different field that happened to be named
    # "lat" — a map centre, an office address.
    if not (37.6 <= lat_f <= 37.9 and -123.2 <= lon_f <= -122.3):
        return None, None
    return lat_f, lon_f
