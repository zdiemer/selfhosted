"""Craigslist — the load-bearing source.

Craigslist serves a no-JS fallback list (`<li class="cl-static-search-result">`)
alongside its React app, and that fallback honours the query string. So the
search step needs no browser at all: one plain GET returns every result, already
filtered by price, bedrooms, and laundry.

The search page gives title, URL, price, and a poster-supplied location string.
Bedrooms, laundry, parking, and coordinates only exist on the detail page, so
each *unseen* listing costs one more cheap GET. That's why `seen` matters: on a
steady day it's a handful of fetches, not two hundred.

Filtering laundry server-side is worth doing even though we re-check it on the
detail page — it cut a live SF query from 339 results to 200, i.e. 139 detail
pages we never request.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import urlencode

from .base import Listing, parse_parking_fee

logger = logging.getLogger(__name__)

NAME = "craigslist"

# Craigslist's own filter codes, from the `laundry=` / `parking=` links it puts
# on every detail page. Mapped onto our normalized tokens.
CL_LAUNDRY = {
    "1": "in_unit",       # w/d in unit
    "2": "hookups_only",  # w/d hookups
    "3": "on_site",       # laundry on site
    "4": "in_building",   # laundry in bldg
    "5": "none",          # no laundry on site
}
TOKEN_TO_CL_LAUNDRY = {
    "in_unit": "1",
    "hookups_only": "2",
    "on_site": "3",
    "in_building": "4",
    "none": "5",
}

CL_PARKING = {
    "1": "carport",
    "2": "garage",       # attached garage
    "3": "garage",       # detached garage
    "4": "off_street",   # off-street parking
    "5": "street",       # street parking
    "6": "valet",        # valet parking
    "7": "none",         # no parking
}

# `sfc` is the San Francisco *city* subarea of the sfbay site — the Peninsula
# and East Bay are different subareas and never appear here.
SEARCH_URL = "https://sfbay.craigslist.org/search/sfc/apa"

_RESULT_RE = re.compile(r'<li class="cl-static-search-result"[^>]*>(.*?)</li>', re.S)
_HREF_RE = re.compile(r'<a href="([^"]+)"')
_TITLE_RE = re.compile(r'<div class="title">(.*?)</div>', re.S)
_PRICE_RE = re.compile(r'<div class="price">\$([0-9,]+)</div>')
_LOCATION_RE = re.compile(r'<div class="location">(.*?)</div>', re.S)

_LAT_RE = re.compile(r'data-latitude="(-?[0-9.]+)"')
_LON_RE = re.compile(r'data-longitude="(-?[0-9.]+)"')
_BR_RE = re.compile(r'<span class="attr important">\s*([0-9]+)\s*BR', re.I)
_STUDIO_RE = re.compile(r'<span class="attr important">\s*(?:0\s*BR|studio)', re.I)
# The separator must allow `&amp;` — these are hrefs in HTML, so every query
# separator after the first is escaped. Matching only `[?&]` silently finds
# nothing and every listing comes back laundry=unknown.
_ATTR_LINK_RE = re.compile(r'(?:[?&]|&amp;)(laundry|parking)=([0-9])"[^>]*>\s*([^<]+?)\s*<')
_BODY_RE = re.compile(r'id="postingbody".*?</section>', re.S)


def _text(raw: str) -> str:
    """Strip tags and unescape the handful of entities Craigslist emits."""
    out = re.sub(r"<[^>]+>", " ", raw)
    for a, b in (("&amp;", "&"), ("&#x27;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        out = out.replace(a, b)
    return re.sub(r"\s+", " ", out).strip()


def _search_url(criteria) -> str:
    params = [
        ("max_price", str(criteria.search.max_effective_rent)),
        ("min_bedrooms", str(criteria.search.min_bedrooms)),
        ("max_bedrooms", str(criteria.search.max_bedrooms)),
        ("sort", "date"),
    ]
    if criteria.search.min_rent:
        params.append(("min_price", str(criteria.search.min_rent)))

    # Push the laundry requirement server-side. Only safe when the rule is
    # `required` — otherwise the filter would hide listings we'd have accepted.
    if criteria.laundry.required:
        for token in sorted(criteria.laundry.accept):
            code = TOKEN_TO_CL_LAUNDRY.get(token)
            if code:
                params.append(("laundry", code))
    return f"{SEARCH_URL}?{urlencode(params)}"


def _parse_detail(html: str, listing: Listing) -> Listing:
    """Fill in bedrooms, laundry, parking, and coordinates from a post page."""
    m = _LAT_RE.search(html)
    if m:
        listing.lat = float(m.group(1))
    m = _LON_RE.search(html)
    if m:
        listing.lon = float(m.group(1))

    if _STUDIO_RE.search(html):
        listing.bedrooms = 0
    else:
        m = _BR_RE.search(html)
        if m:
            listing.bedrooms = int(m.group(1))

    # The attr links carry Craigslist's own codes, which beats reading prose.
    for kind, code, label in _ATTR_LINK_RE.findall(html):
        if kind == "laundry":
            listing.laundry = CL_LAUNDRY.get(code, listing.laundry)
        elif kind == "parking":
            listing.parking = CL_PARKING.get(code, listing.parking)

    m = _BODY_RE.search(html)
    body = _text(m.group(0)) if m else ""
    listing.body = body
    # A parking fee is never structured — it's always buried in the prose.
    listing.parking_fee = parse_parking_fee(f"{listing.title} {body}")
    return listing


class Craigslist:
    name = NAME

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        url = _search_url(criteria)
        logger.info("[%s] search %s", NAME, url)
        html = fetcher.get(url)
        if not html:
            raise RuntimeError("search page fetch failed")

        blocks = _RESULT_RE.findall(html)
        logger.info("[%s] %d results on the search page", NAME, len(blocks))

        cfg = criteria.sources.get(NAME, {})
        budget = int(cfg.get("max_detail_fetches", criteria.max_detail_fetches))
        fetched = skipped = offsite = 0

        for block in blocks:
            m = _HREF_RE.search(block)
            if not m:
                continue
            href = m.group(1)

            # The `sfc` subarea does NOT constrain the static result list —
            # a live SF query came back with Berkeley, Brooklyn and the Bronx
            # mixed in (70 of 201 results). The universal /view/d/ slug is
            # prefixed with the post's own city, which is the cheapest reliable
            # way to drop them before spending a detail fetch. Coordinates are
            # still the authority once we have them (see main.evaluate).
            slug = href.rsplit("/view/d/", 1)[-1]
            if not slug.startswith("san-francisco"):
                offsite += 1
                continue

            external_id = href.rstrip("/").rsplit("/", 1)[-1]

            title_m = _TITLE_RE.search(block)
            price_m = _PRICE_RE.search(block)
            loc_m = _LOCATION_RE.search(block)

            listing = Listing(
                source=NAME,
                external_id=external_id,
                url=href,
                title=_text(title_m.group(1)) if title_m else "",
                price=int(price_m.group(1).replace(",", "")) if price_m else None,
                stated_neighborhood=_text(loc_m.group(1)) if loc_m else None,
            )

            # Already in the DB: re-yield with the fresh price so a price drop
            # is caught, but don't spend a request re-reading the detail page.
            if external_id in seen:
                yield listing
                continue

            if fetched >= budget:
                skipped += 1
                continue

            fetcher.sleep()
            detail = fetcher.get(href)
            fetched += 1
            if not detail:
                logger.debug("[%s] detail fetch failed for %s", NAME, href)
                yield listing
                continue
            yield _parse_detail(detail, listing)

        if offsite:
            logger.info("[%s] dropped %d results outside San Francisco", NAME, offsite)
        if skipped:
            # Never let a cap look like "that's everything there was".
            logger.warning(
                "[%s] detail-fetch cap of %d reached — %d new listings left "
                "unexamined this run; they'll be picked up next run",
                NAME, budget, skipped,
            )
        logger.info("[%s] fetched %d detail pages", NAME, fetched)
