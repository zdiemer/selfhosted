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

import json
import logging
import re
from typing import Iterator
from urllib.parse import urlencode

import areas as area_registry

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

# One search per area, because sfbay's subareas really are separate result sets:
# `sfc` is the San Francisco city, `nby` the North Bay, `pen` the Peninsula. The
# subarea narrows it to a region; the per-area city-slug list (areas.py) narrows
# that region to the towns we actually want, since `nby` is mostly Sonoma.
SEARCH_URL = "https://sfbay.craigslist.org/search/{subarea}/apa"

# Craigslist's own search API, the one its React front-end calls. The static
# HTML carries no photos at all — only an `imageConfig` naming the CDN — but
# SAPI returns an image reference per result, so ONE extra call photographs the
# whole run without a single browser render. Discovered via the sibling talaria
# repo, which uses this endpoint wholesale.
#
# `searchPath=sfc/apa` is the part that matters: the default batch spans all of
# sfbay and returned 9 San Francisco posts out of 360, while this returns the
# complete SF set (263 of 263) in one page.
SAPI_URL = "https://sapi.craigslist.org/web/v8/postings/search/full"
SAPI_BATCH = "1-0-360-0-0"

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
# Craigslist has no address field, but posters put one in the title often
# enough to be worth reading: "755 O'Farrell Street #44", "1386 Page St".
# An address beats the poster-placed map pin, which is frequently wrong.
_ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Za-z0-9'\.\-]+(?:\s+[A-Za-z0-9'\.\-]+){0,3}?\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Way|Pl|Place|Ct|Ter|Terrace|Ln|Lane)\b)",
    re.I,
)


def _text(raw: str) -> str:
    """Strip tags and unescape the handful of entities Craigslist emits."""
    out = re.sub(r"<[^>]+>", " ", raw)
    for a, b in (("&amp;", "&"), ("&#x27;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        out = out.replace(a, b)
    return re.sub(r"\s+", " ", out).strip()


def _search_url(criteria, area) -> str:
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
    return f"{SEARCH_URL.format(subarea=area.cl_subarea)}?{urlencode(params)}"


def _slug(href: str) -> str:
    """`.../view/d/<slug>/<id>` -> `<slug>`. The join key between the static
    result list and SAPI, which returns the same slug per posting."""
    parts = href.rstrip("/").split("/")
    return parts[-2] if len(parts) >= 2 else ""


def image_index(fetcher, criteria, area) -> dict[str, str]:
    """slug -> photo URL, from one SAPI call. Empty dict on any failure.

    Photos are a nice-to-have: a broken index must cost the pictures, never the
    listings, so everything here is best-effort and returns {} rather than
    raising.
    """
    params = {
        "batch": SAPI_BATCH,
        "searchPath": f"{area.cl_subarea}/apa",
        "lang": "en",
        "cc": "us",
        "min_bedrooms": str(criteria.search.min_bedrooms),
        "max_bedrooms": str(criteria.search.max_bedrooms),
        "max_price": str(criteria.search.max_effective_rent),
    }
    raw = fetcher.get(f"{SAPI_URL}?{urlencode(params)}")
    if not raw:
        logger.info("[%s] SAPI unavailable — cards will have no photos", NAME)
        return {}
    try:
        items = json.loads(raw)["data"]["items"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.info("[%s] SAPI payload changed (%s) — no photos this run", NAME, exc)
        return {}

    out: dict[str, str] = {}
    for item in items:
        try:
            # Positional array. [7] is [photoCount, ref, ...]; [8] is
            # [_, urlSlug]. Layout documented in talaria's
            # scraper/types/craigslist/listing.py.
            refs = item[7] if len(item) > 7 else None
            slug = item[8][1] if len(item) > 8 and len(item[8]) > 1 else None
            if not slug or not isinstance(refs, list) or len(refs) < 2:
                continue
            ref = refs[1]
            if not isinstance(ref, str) or ref in ("", "0"):
                continue
            # Refs come prefixed with a size-set id, e.g. "3:00808_abc_0dI099".
            ref = ref.split(":", 1)[1] if ":" in ref else ref
            out[slug] = f"https://images.craigslist.org/{ref}_600x450.jpg"
        except (IndexError, TypeError):
            continue
    logger.info("[%s] %d photos indexed from SAPI", NAME, len(out))
    return out


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

    # No photo, deliberately. Craigslist's detail HTML carries only an
    # `imageConfig` (CDN host + sizes) — the photo IDs arrive over XHR, so the
    # only way to get one is a browser render per listing. At ~150 detail
    # fetches a run that costs more than the photo is worth, and the cheap
    # plain-HTTP path is the reason this source is affordable at all. The run
    # page renders these cards compactly rather than reserving empty space.

    m = _BODY_RE.search(html)
    body = _text(m.group(0)) if m else ""
    listing.body = body
    # A parking fee is never structured — it's always buried in the prose.
    listing.parking_fee = parse_parking_fee(f"{listing.title} {body}")
    return listing


class Craigslist:
    name = NAME

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        cfg = criteria.sources.get(NAME, {})
        # One budget for the whole source, not one per area: the cap exists to
        # bound how much traffic a run generates, and searching three areas
        # shouldn't triple that.
        budget = int(cfg.get("max_detail_fetches", criteria.max_detail_fetches))
        state = {"fetched": 0, "skipped": 0}

        for area in area_registry.enabled(criteria):
            yield from self._search_area(fetcher, criteria, seen, area, budget, state)

        if state["skipped"]:
            # Never let a cap look like "that's everything there was".
            logger.warning(
                "[%s] detail-fetch cap of %d reached — %d new listings left "
                "unexamined this run; they'll be picked up next run",
                NAME, budget, state["skipped"],
            )
        logger.info("[%s] fetched %d detail pages", NAME, state["fetched"])

    def _search_area(self, fetcher, criteria, seen, area, budget, state) -> Iterator[Listing]:
        url = _search_url(criteria, area)
        logger.info("[%s/%s] search %s", NAME, area.key, url)
        html = fetcher.get(url)
        if not html:
            raise RuntimeError(f"search page fetch failed ({area.key})")

        blocks = _RESULT_RE.findall(html)
        logger.info("[%s/%s] %d results on the search page", NAME, area.key, len(blocks))
        photos = image_index(fetcher, criteria, area)

        offsite = 0

        for block in blocks:
            m = _HREF_RE.search(block)
            if not m:
                continue
            href = m.group(1)

            # The subarea does NOT fully constrain the static result list — a
            # live SF query came back with Berkeley, Brooklyn and the Bronx
            # mixed in (70 of 201 results) — and where it does hold, the region
            # is wider than the towns we want: `nby` was 166 Santa Rosa posts
            # out of 348. The universal /view/d/ slug is prefixed with the
            # post's own city, which is the cheapest reliable way to drop those
            # before spending a detail fetch. Coordinates are still the
            # authority once we have them (see main.evaluate).
            slug = href.rsplit("/view/d/", 1)[-1]
            if not area_registry.matches_slug(area, slug):
                offsite += 1
                continue

            external_id = href.rstrip("/").rsplit("/", 1)[-1]

            title_m = _TITLE_RE.search(block)
            price_m = _PRICE_RE.search(block)
            loc_m = _LOCATION_RE.search(block)

            title_text = _text(title_m.group(1)) if title_m else ""
            addr_m = _ADDRESS_RE.search(title_text)
            listing = Listing(
                source=NAME,
                external_id=external_id,
                url=href,
                address=addr_m.group(1) if addr_m else None,
                title=title_text,
                price=int(price_m.group(1).replace(",", "")) if price_m else None,
                stated_neighborhood=_text(loc_m.group(1)) if loc_m else None,
                image_url=photos.get(_slug(href)),
                area=area.key,
            )

            # Already in the DB: re-yield with the fresh price so a price drop
            # is caught, but don't spend a request re-reading the detail page.
            if external_id in seen:
                yield listing
                continue

            if state["fetched"] >= budget:
                state["skipped"] += 1
                continue

            fetcher.sleep()
            detail = fetcher.get(href)
            state["fetched"] += 1
            if not detail:
                logger.debug("[%s] detail fetch failed for %s", NAME, href)
                yield listing
                continue
            yield _parse_detail(detail, listing)

        if offsite:
            logger.info(
                "[%s/%s] dropped %d results from other towns in the subarea",
                NAME, area.key, offsite,
            )
