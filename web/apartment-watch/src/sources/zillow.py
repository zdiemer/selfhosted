"""Zillow — the search-result card blob, plus a detail fetch for amenities.

Zillow's list page embeds one dict per result carrying `detailUrl`, `latLong`,
`address`, and a `units` array of `{price, beds}`. That's enough to price and
place a listing, but **not** enough to judge laundry — which is a hard rule. So
unlike Zumper and Apartments.com, this source has to open detail pages, and each
one costs a full browser navigation.

That makes Zillow the most expensive source per listing, which is why its
detail-fetch budget is small and why it's worth having Craigslist carry the
bulk of the search.

HotPads is Zillow-owned and shares this inventory; it is *not* implemented as a
separate source because PerimeterX refuses it even from a warmed browser
session, and it would add nothing Zillow doesn't already have.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from .base import Listing, parse_laundry, parse_parking, parse_parking_fee
from .embedded import as_bedrooms, as_int, coords, first, json_blobs, walk

logger = logging.getLogger(__name__)

NAME = "zillow"
ORIGIN = "https://www.zillow.com"


def _search_url(criteria) -> str:
    return f"{ORIGIN}/san-francisco-ca/rentals/"


def _cards(html: str) -> list[dict]:
    """Result cards: anything with a detailUrl and a location or unit list."""
    out, seen_urls = [], set()
    for blob in json_blobs(html):
        for d in walk(blob):
            url = d.get("detailUrl")
            if not isinstance(url, str) or not url:
                continue
            if not ("latLong" in d or "units" in d or "address" in d):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(d)
    return out


def _cheapest_unit(card: dict, min_beds: int, max_beds: int) -> tuple[int | None, int | None]:
    """(price, beds) of the cheapest unit within the bedroom range.

    A Zillow "listing" is often a whole building. Taking the building's headline
    rent would compare a 2br price against a 1br budget, so pick the cheapest
    unit that actually matches what we're looking for.
    """
    units = card.get("units")
    best: tuple[int, int] | None = None
    if isinstance(units, list):
        for u in units:
            if not isinstance(u, dict):
                continue
            beds = as_bedrooms(u.get("beds"))
            price = as_int(u.get("price"))
            if price is None or beds is None:
                continue
            if not (min_beds <= beds <= max_beds):
                continue
            if best is None or price < best[0]:
                best = (price, beds)
    if best:
        return best
    # No unit breakdown — fall back to the card's own price fields.
    price = as_int(first(card, "minBaseRent", "price", "unformattedPrice", "priceLabel"))
    beds = as_bedrooms(first(card, "beds", "bedrooms", "minBeds"))
    return price, beds


class Zillow:
    name = NAME

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        url = _search_url(criteria)
        logger.info("[%s] search %s", NAME, url)
        # Cold browser session is enough; warming this 1.9MB page up front
        # stalls for minutes and buys nothing. `origin` is the retry path.
        html = fetcher.get(url, stealth_first=True, origin=ORIGIN)
        if not html:
            raise RuntimeError("search page fetch failed (PerimeterX not cleared)")

        cards = _cards(html)
        logger.info("[%s] %d result cards", NAME, len(cards))

        cfg = criteria.sources.get(NAME, {})
        budget = int(cfg.get("max_detail_fetches", 12))
        fetched = skipped = 0

        for card in cards:
            href = str(card.get("detailUrl") or "")
            if href.startswith("/"):
                href = ORIGIN + href
            if not href.startswith("http"):
                continue
            external_id = str(first(card, "zpid", "id", "providerListingId", default="") or href)
            external_id = re.sub(r"[^A-Za-z0-9_.-]", "_", external_id)[:120]

            price, beds = _cheapest_unit(
                card, criteria.search.min_bedrooms, criteria.search.max_bedrooms
            )
            lat, lon = coords(card)
            listing = Listing(
                source=NAME,
                external_id=external_id,
                url=href,
                title=str(first(card, "buildingName", "statusText", "address", default="") or ""),
                price=price,
                bedrooms=beds,
                lat=lat,
                lon=lon,
                stated_neighborhood=str(first(card, "address", "addressStreet", default="") or "") or None,
            )

            # Cheap pre-filter: don't spend a browser navigation on a listing
            # that already fails on price or bedrooms.
            if price is not None and price > criteria.search.max_effective_rent:
                yield listing
                continue
            if beds is not None and not (
                criteria.search.min_bedrooms <= beds <= criteria.search.max_bedrooms
            ):
                yield listing
                continue
            if external_id in seen:
                yield listing
                continue

            if fetched >= budget:
                skipped += 1
                yield listing
                continue

            fetcher.sleep()
            detail = fetcher.get(href, stealth_first=True)
            fetched += 1
            if not detail:
                yield listing
                continue

            text = re.sub(r"<[^>]+>", " ", detail)
            text = re.sub(r"\s+", " ", text)[:60_000]
            listing.laundry = parse_laundry(text)
            listing.parking = parse_parking(text)
            listing.parking_fee = parse_parking_fee(text)
            listing.body = text[:4000]
            yield listing

        if skipped:
            logger.warning(
                "[%s] detail-fetch cap of %d reached — %d candidates left "
                "unexamined this run", NAME, budget, skipped,
            )
        logger.info("[%s] fetched %d detail pages", NAME, fetched)
