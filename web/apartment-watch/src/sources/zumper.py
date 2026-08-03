"""Zumper — the React state's `Listable` objects, behind an F5 "Client Challenge".

Zumper ships two descriptions of the same results: schema.org JSON-LD, and the
objects it hands its own frontend. The frontend ones win by a mile — they carry
`min_price`, `min_bedrooms`, `lat`/`lng`, `neighborhood_name`, and
`amenity_tags` (where laundry lives), so a listing is fully judged without a
single detail-page fetch. The JSON-LD has no price at all.

Field names here have been stable for years because they're an API contract,
not markup. A reskin doesn't touch them.

PadMapper is the same company running the same inventory — every object even
carries its `padmapper_url` — which is why there's no separate PadMapper source.
"""

from __future__ import annotations

import logging
from typing import Iterator

from .base import Listing, parse_laundry, parse_parking, parse_parking_fee
from .embedded import json_blobs, walk

logger = logging.getLogger(__name__)

NAME = "zumper"
ORIGIN = "https://www.zumper.com"


def _search_url(criteria) -> str:
    return (
        f"{ORIGIN}/apartments-for-rent/san-francisco-ca"
        f"?max={criteria.search.max_effective_rent}"
        f"&min-beds={criteria.search.min_bedrooms}"
        f"&max-beds={criteria.search.max_bedrooms}"
    )


def _listables(html: str) -> list[dict]:
    """The result objects. `listing_id` + `address` is a tight enough shape."""
    out, seen = [], set()
    for blob in json_blobs(html):
        for d in walk(blob):
            lid = d.get("listing_id")
            if lid is None or "address" not in d:
                continue
            if lid in seen:
                continue
            seen.add(lid)
            out.append(d)
    return out


class Zumper:
    name = NAME

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        url = _search_url(criteria)
        logger.info("[%s] search %s", NAME, url)
        # The cheap tier is pointless here: plain HTTP always gets the challenge.
        # A cold browser session clears it; `origin` is only the retry path.
        html = fetcher.get(url, stealth_first=True, origin=ORIGIN)
        if not html:
            raise RuntimeError("search page fetch failed (challenge not cleared)")

        rows = _listables(html)
        logger.info("[%s] %d listings in page state", NAME, len(rows))

        for row in rows:
            if str(row.get("city") or "").lower() not in ("san francisco", ""):
                continue

            path = str(row.get("url") or "")
            if not path:
                continue

            tags = list(row.get("amenity_tags") or []) + list(row.get("building_amenity_tags") or [])
            amenities = " ; ".join(str(t) for t in tags)
            title = str(row.get("title") or row.get("building_name") or "")
            blob = f"{amenities} ; {row.get('short_description') or ''}"

            # min_* rather than max_*: a building spans several floorplans and
            # we want the cheapest one that could match, not the penthouse.
            price = row.get("min_price")
            beds = row.get("min_bedrooms")

            yield Listing(
                source=NAME,
                external_id=str(row["listing_id"]),
                url=path if path.startswith("http") else ORIGIN + path,
                title=title,
                price=int(price) if isinstance(price, (int, float)) else None,
                bedrooms=int(beds) if isinstance(beds, (int, float)) else None,
                laundry=parse_laundry(amenities),
                parking=parse_parking(amenities),
                parking_fee=parse_parking_fee(blob),
                lat=float(row["lat"]) if isinstance(row.get("lat"), (int, float)) else None,
                lon=float(row["lng"]) if isinstance(row.get("lng"), (int, float)) else None,
                stated_neighborhood=str(row.get("neighborhood_name") or "") or None,
                body=blob,
            )
