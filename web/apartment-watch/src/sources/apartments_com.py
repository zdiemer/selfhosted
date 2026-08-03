"""Apartments.com — Akamai Bot Manager, cleared by warming up first.

A cold GET of a search URL returns a 2.5KB Akamai challenge shell every single
time, browser or not. Loading the homepage, waiting ~12s for the sensor script
to post its telemetry, and *then* navigating to the search page returns the real
800KB listing page. That's what `warm_up_first=True` buys, and without it this
source is 100% blocked. It's the only source that needs it — Zumper and Zillow
both answer a cold browser session, and warming them costs time for nothing.

The payload is schema.org: one `ApartmentComplex` per result with `geo`
coordinates, a `PostalAddress`, and `amenityFeature` entries. Prices live in the
sibling `Product`/`RealEstateListing` and in the card DOM.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from .base import Listing, parse_bedrooms, parse_laundry, parse_parking, parse_parking_fee
from .embedded import as_int, coords, first, json_blobs, walk

logger = logging.getLogger(__name__)

NAME = "apartments_com"
ORIGIN = "https://www.apartments.com"


def _search_url(criteria) -> str:
    beds = "studios" if criteria.search.max_bedrooms == 0 else f"min-{max(criteria.search.min_bedrooms,1)}-bedrooms"
    if criteria.search.min_bedrooms == 0 and criteria.search.max_bedrooms >= 1:
        # Their URL grammar has no "studio OR 1br"; the unfiltered city page
        # plus our own bedroom test covers both without a second request.
        return f"{ORIGIN}/san-francisco-ca/under-{criteria.search.max_effective_rent}/"
    return f"{ORIGIN}/san-francisco-ca/{beds}-under-{criteria.search.max_effective_rent}/"


def _amenity_text(d: dict) -> str:
    feats = d.get("amenityFeature") or []
    names = []
    for f in feats:
        if isinstance(f, dict) and f.get("name"):
            names.append(("no " if f.get("value") is False else "") + str(f["name"]))
    return " ; ".join(names)


def _dom_cards(html: str) -> dict[str, dict]:
    """listingId -> {price, beds} scraped from the placard markup.

    JSON-LD gives location and amenities but not the rent, so this fills the
    gap. Anchored on `data-listingid`, which has outlived several redesigns.
    """
    out: dict[str, dict] = {}
    for m in re.finditer(r'data-listingid="([^"]+)"', html):
        lid = m.group(1)
        window = html[m.start() : m.start() + 4000]
        text = re.sub(r"<[^>]+>", " ", window)
        text = re.sub(r"\s+", " ", text)
        price = re.search(r"\$\s*([0-9][0-9,]{2,6})", text)
        out[lid] = {
            "price": int(price.group(1).replace(",", "")) if price else None,
            "beds": parse_bedrooms(text),
            "text": text[:1500],
        }
    return out


class ApartmentsCom:
    name = NAME

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        url = _search_url(criteria)
        logger.info("[%s] search %s", NAME, url)
        # warm_up_first is mandatory here, not a fallback: a cold request
        # returns a 2.5KB Akamai challenge shell 100% of the time.
        html = fetcher.get(url, stealth_first=True, origin=ORIGIN, warm_up_first=True)
        if not html:
            raise RuntimeError("search page fetch failed (Akamai challenge not cleared)")

        cards = _dom_cards(html)
        complexes = [
            d for b in json_blobs(html) for d in walk(b)
            if str(d.get("@type")) == "ApartmentComplex" and d.get("@id")
        ]
        logger.info(
            "[%s] %d complexes in JSON-LD, %d DOM cards", NAME, len(complexes), len(cards)
        )

        for c in complexes:
            at_id = str(c.get("@id") or "")
            href = at_id.split("#")[0]
            if not href.startswith("http"):
                continue
            address = c.get("address") if isinstance(c.get("address"), dict) else {}
            locality = str(address.get("addressLocality") or "")
            # The SF page includes Daly City, Brisbane, South SF results.
            if locality and locality.lower() != "san francisco":
                continue

            external_id = href.rstrip("/").rsplit("/", 1)[-1]
            card = cards.get(external_id, {})
            amenities = _amenity_text(c)
            blob = f"{amenities} ; {card.get('text', '')}"
            lat, lon = coords(c)

            yield Listing(
                source=NAME,
                external_id=external_id,
                url=href,
                title=str(first(c, "name", default="") or ""),
                price=card.get("price") or as_int(first(c, "price", "offers")),
                bedrooms=card.get("beds"),
                laundry=parse_laundry(blob),
                parking=parse_parking(blob),
                parking_fee=parse_parking_fee(blob),
                lat=lat,
                lon=lon,
                stated_neighborhood=str(address.get("streetAddress") or "") or None,
                body=blob,
            )
