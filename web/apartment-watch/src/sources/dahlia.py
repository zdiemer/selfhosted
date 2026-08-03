"""DAHLIA — San Francisco's official Below Market Rate housing portal.

This is the source that matters most on a tight budget, and it's the one no
other site carries. BMR units are income-restricted and rent well under market
— a 50% AMI 1BR at $1,768 against a $3,563 market average — and they're
allocated by lottery, not by who emails first.

Which changes what "matching" means here:

* **Rent below market is the point**, not a red flag. The scam filter's price
  rules would reject every one of these as bait, so this source is marked
  trusted and skips them. It's a government portal; there is nothing to spoof.
* **There is an income floor as well as a ceiling.** BMR units require a minimum
  income (roughly 2x rent annualised) *and* cap the maximum. Earning too much
  disqualifies you exactly like earning too little. The API gives both as
  monthly figures.
* **Deadlines are the whole game.** A lottery closes on a date; miss it and the
  unit is gone regardless of fit. The digest leads with days remaining.

No amenity data is published, so laundry can't be checked — see `trusted` in
main.evaluate for how that's handled rather than silently rejecting everything.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

from .base import Listing

logger = logging.getLogger(__name__)

NAME = "dahlia"
API = "https://housing.sfgov.org/api/v1/listings.json"
LISTING_URL = "https://housing.sfgov.org/listings/{id}"

# "Studio" -> 0, "1 BR" -> 1, and so on.
def _beds(unit_type: str) -> int | None:
    t = (unit_type or "").strip().lower()
    if "studio" in t or "sro" in t:
        return 0
    for n in range(1, 6):
        if t.startswith(f"{n} "):
            return n
    return None


def _is_rental(listing: dict) -> bool:
    return "rental" in str(listing.get("Tenure") or "").lower()


def _days_left(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        due = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (due - datetime.now(timezone.utc)).days


class Dahlia:
    name = NAME
    trusted = True   # skip the scam filter and the laundry requirement

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        cfg = criteria.sources.get(NAME, {})
        # Annual income, as a range — it decides qualification in both
        # directions, so a guess that's too precise is worse than a range.
        income_min = float(cfg.get("annual_income_min") or 0)
        income_max = float(cfg.get("annual_income_max") or income_min or 0)
        if not income_max:
            logger.warning("[%s] no annual_income_min/max set — not filtering on income", NAME)

        logger.info("[%s] fetching %s", NAME, API)
        try:
            with urllib.request.urlopen(API, timeout=45) as resp:
                payload = json.loads(resp.read() or b"{}")
        except Exception as exc:
            raise RuntimeError(f"DAHLIA API fetch failed: {exc}") from exc

        rows = payload.get("listings") or []
        rentals = [r for r in rows if _is_rental(r)]
        logger.info("[%s] %d listings, %d rentals", NAME, len(rows), len(rentals))

        emitted = 0
        for row in rentals:
            summaries = (row.get("unitSummaries") or {}).get("general") or []
            due_in = _days_left(row.get("Application_Due_Date"))
            status = str(row.get("Status") or "")

            # A passed deadline does NOT mean closed here, and treating it that
            # way threw away most of the source: of 57 rentals, 55 were past
            # their due date, but 46 of those are "Lease Up" (lottery finished,
            # units filling from the existing list) and 9 are still "Active"
            # with units available and an open waitlist. Drop the former, keep
            # the latter and say the deadline has passed.
            stale = due_in is not None and due_in < 0
            if stale and status.lower() != "active":
                continue

            for unit in summaries:
                beds = _beds(unit.get("unitType"))
                if beds is None:
                    continue
                if not (criteria.search.min_bedrooms <= beds <= criteria.search.max_bedrooms):
                    continue

                rent = unit.get("minMonthlyRent")
                if not isinstance(rent, (int, float)) or rent <= 0:
                    continue

                # Income limits are MONTHLY in this API.
                lo = unit.get("absoluteMinIncome")
                hi = unit.get("absoluteMaxIncome")
                if income_max and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                    lo_year, hi_year = lo * 12, hi * 12
                    # Qualify if her income range overlaps the unit's band at all;
                    # borderline cases are surfaced rather than silently dropped.
                    if income_max < lo_year or income_min > hi_year:
                        continue
                    # Flag near-misses as well as overlaps. BMR limits are on
                    # gross HOUSEHOLD income and the portal's definition may not
                    # match the number here, so sitting within 5% of either
                    # bound is worth checking before applying rather than
                    # trusting.
                    margin = 0.05
                    borderline = (
                        income_min < lo_year
                        or income_max > hi_year
                        or income_min < lo_year * (1 + margin)
                        or income_max > hi_year * (1 - margin)
                    )
                else:
                    lo_year = hi_year = None
                    borderline = False

                bits = [row.get("Name") or "BMR unit"]
                if stale:
                    bits.append("deadline passed - ask about the waitlist")
                elif due_in is not None:
                    bits.append(f"apply in {due_in}d" if due_in else "apply TODAY")
                if unit.get("maxQualifyingAMI"):
                    bits.append(f"{unit['maxQualifyingAMI']:.0f}% AMI")
                if borderline and hi_year:
                    bits.append(f"income {lo_year/1000:.0f}-{hi_year/1000:.0f}k — check")
                if row.get("hasWaitlist"):
                    bits.append("waitlist")

                if stale:
                    note = "BMR waitlist?"
                elif due_in is not None:
                    note = f"BMR apply {due_in}d" if due_in else "BMR apply TODAY"
                else:
                    note = "BMR"
                if unit.get("maxQualifyingAMI"):
                    note += f" {unit['maxQualifyingAMI']:.0f}%AMI"
                if borderline:
                    note += " check-income"

                emitted += 1
                yield Listing(
                    source=NAME,
                    external_id=f"{row.get('listingID')}:{unit.get('unitType')}",
                    url=LISTING_URL.format(id=row.get("listingID")),
                    title=" | ".join(str(b) for b in bits),
                    price=int(rent),
                    bedrooms=beds,
                    # Amenities aren't published; `trusted` stops this being a
                    # rejection.
                    laundry="unknown",
                    parking="unknown",
                    address=row.get("Building_Street_Address") or None,
                    body=f"BMR / below market rate. {row.get('Name') or ''}",
                    note=note,
                )

        logger.info("[%s] %d qualifying unit type(s)", NAME, emitted)
