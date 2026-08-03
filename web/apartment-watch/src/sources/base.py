"""The contract every source implements, plus the shared text parsers.

A source's whole job is to turn a site into `Listing` objects with **normalized**
laundry/parking tokens. All the judgement about whether a listing qualifies
lives in main.py and applies identically to all four sites — a source that
leaks site-specific vocabulary upward defeats that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterator, Protocol

logger = logging.getLogger(__name__)


@dataclass
class Listing:
    source: str
    external_id: str
    url: str
    title: str = ""
    price: int | None = None
    bedrooms: int | None = None
    laundry: str = "unknown"     # config.LAUNDRY_TOKENS
    parking: str = "unknown"     # config.PARKING_TOKENS
    parking_fee: int | None = None
    lat: float | None = None
    lon: float | None = None
    # A street address, and a poster-supplied NEIGHBOURHOOD label. Keep these
    # apart: feeding an address into the neighbourhood matcher makes a unit on
    # "Bayview Ave" look like it's in the Bayview and get excluded.
    address: str | None = None
    stated_neighborhood: str | None = None
    body: str = ""
    # Short, source-specific text that must survive into the SMS (BMR deadlines).
    note: str = ""
    # First photo, hot-linked by the web view. Optional everywhere.
    image_url: str | None = None

    # Filled in by main.py during evaluation.
    neighborhood: str | None = None
    effective_price: int | None = None
    rent_controlled: int | None = None
    year_built: int | None = None


class Source(Protocol):
    name: str

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        """Yield listings. Must not raise — main.py catches, but a source that
        handles its own per-listing failures loses one listing instead of the
        rest of the page.
        """
        ...


# -- shared text parsing ---------------------------------------------------

# Ordered most-specific first: "washer/dryer in unit" must win over a bare
# "laundry" appearing later in the same blob.
_LAUNDRY_PATTERNS: list[tuple[str, str]] = [
    (r"w/?d\s*in\s*unit|washer\s*/?\s*dryer\s*in\s*unit|in[- ]unit\s+(?:laundry|washer)"
     r"|laundry\s+in\s+unit|in\s+unit\s+laundry", "in_unit"),
    (r"laundry\s+in\s+bl?dg|laundry\s+in\s+building|in[- ]building\s+laundry"
     r"|shared\s+laundry|common\s+laundry|laundry\s+room", "in_building"),
    (r"laundry\s+on\s*[- ]?\s*site|on[- ]site\s+laundry|laundry\s+facilit", "on_site"),
    (r"w/?d\s*hookups?|washer\s*/?\s*dryer\s*hookups?|laundry\s+hookups?", "hookups_only"),
    (r"no\s+laundry(\s+on\s*[- ]?\s*site)?|laundry:\s*none", "none"),
]

_PARKING_PATTERNS: list[tuple[str, str]] = [
    (r"\bvalet\s+parking\b", "valet"),
    (r"attached\s+garage|detached\s+garage|\bgarage\b|garage\s+parking"
     r"|parking\s+garage|deeded\s+parking", "garage"),
    (r"\bcarport\b", "carport"),
    (r"off[- ]street\s+parking|off\s+street\s+parking|assigned\s+parking"
     r"|dedicated\s+parking|parking\s+(?:space|spot)\s+included|\bparking\s+included\b", "off_street"),
    (r"street\s+parking\s+only|\bstreet\s+parking\b|permit\s+parking", "street"),
    (r"no\s+parking\b|parking:\s*none", "none"),
]

# "$250/mo parking", "parking is $200 a month", "parking: $175/month".
_PARKING_FEE_PATTERNS = [
    r"parking[^.$\n]{0,40}?\$\s*([0-9]{2,4})\s*(?:/|\s+per\s+|\s+a\s+)?\s*(?:mo|month)",
    r"\$\s*([0-9]{2,4})\s*(?:/|\s+per\s+|\s+a\s+)?\s*(?:mo|month)[^.$\n]{0,30}?parking",
    r"parking\s*(?:fee|rent|cost)?\s*[:\-]?\s*\$\s*([0-9]{2,4})\b",
    r"garage\s*(?:fee|rent|parking|space)?\s*[:\-]?\s*\$\s*([0-9]{2,4})\b",
]


def parse_laundry(text: str) -> str:
    """Normalize any free text to a laundry token. 'unknown' when it says nothing.

    Order matters: "no laundry on site" contains "laundry on site", so the
    negative pattern is checked before returning a positive. Handled by putting
    `none` last but requiring the explicit negation prefix in its pattern, and
    by checking negation up front here.
    """
    if not text:
        return "unknown"
    t = re.sub(r"\s+", " ", text.lower())

    if re.search(r"no\s+laundry|laundry:\s*none|w/?d:\s*none", t):
        return "none"
    for pattern, token in _LAUNDRY_PATTERNS:
        if re.search(pattern, t):
            return token
    return "unknown"


def parse_parking(text: str) -> str:
    if not text:
        return "unknown"
    t = re.sub(r"\s+", " ", text.lower())

    if re.search(r"no\s+parking|parking:\s*none", t):
        return "none"
    for pattern, token in _PARKING_PATTERNS:
        if re.search(pattern, t):
            return token
    return "unknown"


def parse_parking_fee(text: str) -> int | None:
    """A stated monthly parking cost, or None.

    None means "not stated", which is very different from zero — main.py
    resolves it via rules.parking.unknown_fee rather than assuming either way.
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.lower())
    for pattern in _PARKING_FEE_PATTERNS:
        m = re.search(pattern, t)
        if m:
            try:
                fee = int(m.group(1))
            except ValueError:
                continue
            # Sanity band: below $20 is noise, above $900 is a second rent
            # figure that happened to sit near the word "parking".
            if 20 <= fee <= 900:
                return fee
    return None


def parse_price(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\$\s*([0-9][0-9,]{2,6})", text)
    if not m:
        return None
    try:
        value = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if 200 <= value <= 100_000 else None


def parse_bedrooms(text: str) -> int | None:
    """Bedroom count, with studio == 0."""
    if not text:
        return None
    t = text.lower()
    if re.search(r"\bstudios?\b|\bbachelor\b|\b0\s*(?:br|bd|bed)\b", t):
        return 0
    m = re.search(r"\b([0-9])\s*(?:br|bd|beds?|bedrooms?)\b", t)
    if m:
        return int(m.group(1))
    return None
