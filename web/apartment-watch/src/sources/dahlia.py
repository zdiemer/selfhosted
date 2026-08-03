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

# HUD household-size adjustments, as a share of the 4-person income limit.
# The API's absoluteMaxIncome is the limit for the unit's MAXIMUM occupancy,
# not for the person reading the text: a 1BR that sleeps 3 reports the
# 3-person ceiling. Rescaling by these is what turns it into her ceiling.
# Verified against the portal: a 50% AMI 1BR reporting $7,879 (3-person)
# rescales to 7879 * 0.70/0.90 = $6,129, which is exactly the 1-person limit
# the site displays.
HOUSEHOLD_FACTOR = {1: 0.70, 2: 0.80, 3: 0.90, 4: 1.00, 5: 1.08, 6: 1.16, 7: 1.24, 8: 1.32}
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


def _due_date(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_left(raw: str | None) -> int | None:
    due = _due_date(raw)
    return None if due is None else (due - datetime.now(timezone.utc)).days


class Dahlia:
    name = NAME
    trusted = True   # skip the scam filter and the laundry requirement

    def search(self, fetcher, criteria, seen: set[str]) -> Iterator[Listing]:
        cfg = criteria.sources.get(NAME, {})
        # Annual income, as a range — it decides qualification in both
        # directions, so a guess that's too precise is worse than a range.
        household = max(1, int(cfg.get("household_size", 1) or 1))
        stale_days = int(cfg.get("stale_days", 60) or 60)
        overage_tolerance = float(cfg.get("income_overage_tolerance", 0.10) or 0)
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
            due = _due_date(row.get("Application_Due_Date"))
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
            # "Active" with a passed deadline is a live waitlist for a few
            # weeks; it is not one seven months later. A live pull had entries
            # 186 and 305 days past due still marked Active, and texting those
            # wastes a trip.
            if stale and -due_in > stale_days:
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

                # Occupancy: a unit with a minimum occupancy above the
                # household size isn't available to that household at all.
                min_occ = unit.get("minOccupancy")
                if isinstance(min_occ, (int, float)) and household > min_occ:
                    pass  # larger household than the minimum is fine
                elif isinstance(min_occ, (int, float)) and household < min_occ:
                    continue

                # Income limits are MONTHLY in this API.
                lo = unit.get("absoluteMinIncome")
                hi = unit.get("absoluteMaxIncome")
                if isinstance(hi, (int, float)):
                    # Rescale the ceiling from the unit's max occupancy to this
                    # household. Without this the ceiling is too generous and
                    # units she cannot qualify for get texted — which is worse
                    # than missing one, because it wastes the trip.
                    max_occ = unit.get("maxOccupancy")
                    f_unit = HOUSEHOLD_FACTOR.get(int(max_occ)) if isinstance(max_occ, (int, float)) else None
                    f_hh = HOUSEHOLD_FACTOR.get(household)
                    if f_unit and f_hh:
                        hi = hi * f_hh / f_unit

                if income_max and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                    lo_year, hi_year = lo * 12, hi * 12
                    # Qualify if her income range overlaps the unit's band at all;
                    # borderline cases are surfaced rather than silently dropped.
                    if income_max < lo_year:
                        continue
                    # Just over the ceiling is worth a phone call, not a silent
                    # drop. BMR limits are on gross HOUSEHOLD income under the
                    # portal's own definition, which may not match the figure
                    # configured here, and the gap is often a couple of percent
                    # on a unit hundreds a month below market. Anything beyond
                    # the tolerance is genuinely out of reach and is dropped.
                    over = income_min > hi_year
                    if over and income_min > hi_year * (1 + overage_tolerance):
                        continue
                    # Flag near-misses as well as overlaps. BMR limits are on
                    # gross HOUSEHOLD income and the portal's definition may not
                    # match the number here, so sitting within 5% of either
                    # bound is worth checking before applying rather than
                    # trusting.
                    margin = 0.05
                    borderline = (
                        over
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

                # Plain English, and the dollar income limits rather than an
                # AMI percentage. "50% AMI" is meaningless without a lookup
                # table; "income $42k-$94k" tells you in one glance whether
                # it's worth clicking. Whoever reads this text should not need
                # to know what BMR, AMI or a lottery is.
                parts = ["city affordable unit"]
                if stale:
                    parts.append("waitlist open")
                elif due_in is not None and due_in <= 0:
                    parts.append("apply TODAY")
                elif due_in is not None:
                    due_on = due.strftime("%-m/%-d") if due else None
                    parts.append(f"apply by {due_on}" if due_on else f"apply within {due_in}d")
                if lo_year and hi_year:
                    parts.append(f"income {lo_year/1000:.0f}-{hi_year/1000:.0f}k")
                if lo_year and hi_year and income_min > hi_year:
                    parts.append(
                        f"you're ~{(income_min / hi_year - 1) * 100:.0f}% over the income cap - worth asking"
                    )
                elif borderline:
                    parts.append("you're near the income limit, check")
                note = ", ".join(parts)

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
                    image_url=row.get("imageURL") or None,
                )

        logger.info("[%s] %d qualifying unit type(s)", NAME, emitted)
