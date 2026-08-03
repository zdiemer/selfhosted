"""Is this building likely rent-controlled?

San Francisco rent control covers buildings with a certificate of occupancy
issued before **13 June 1979**. Over a multi-year tenancy that's worth far more
than a hundred dollars of starting rent: increases are capped at a small annual
allowable percentage instead of tracking the market.

Costa-Hawkins carves out single-family homes and condominiums, so unit count
matters as well as year. This flags *likely* coverage from the assessor's
year-built and use code — it is a heuristic on public data, not a legal
determination, and the digest says "RC?" rather than "RC" for that reason.

Two ways in, because sources differ in what they publish:

* a street address (Zumper, Apartments.com, DAHLIA) — matched against the
  assessor's padded `property_location`
* latitude/longitude (Craigslist, which never gives a street address) — nearest
  parcel within a short radius

Every answer is cached forever in SQLite, keyed by whichever lookup was used.
Buildings do not change their year of construction.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

DATASET = "https://data.sfgov.org/resource/wv5m-vpq2.json"
CUTOFF_YEAR = 1979

# Costa-Hawkins: detached single-family homes and condos are exempt however old
# the building is.
_EXEMPT_USE = re.compile(r"single\s*family|condominium|condo\b", re.I)


def _query(where: str, limit: int = 5) -> list[dict]:
    params = {
        "$select": "property_location,year_property_built,use_definition,number_of_units,closed_roll_year",
        "$where": where,
        "$order": "closed_roll_year DESC",
        "$limit": str(limit),
    }
    url = f"{DATASET}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read() or b"[]")
    except Exception as exc:
        logger.debug("rentcontrol: query failed (%s)", exc)
        return []


def _verdict(row: dict) -> dict | None:
    raw_year = str(row.get("year_property_built") or "").strip()
    if not raw_year.isdigit():
        return None
    year = int(raw_year)
    # 0 and absurd values mean "not recorded", not "built in year zero".
    if not 1800 <= year <= 2100:
        return None
    try:
        units = int(float(row.get("number_of_units") or 0))
    except (TypeError, ValueError):
        units = 0
    use = str(row.get("use_definition") or "")

    exempt = bool(_EXEMPT_USE.search(use)) or units == 1
    controlled = year < CUTOFF_YEAR and not exempt
    return {
        "year_built": year,
        "units": units,
        "use": use,
        "controlled": controlled,
        "address": str(row.get("property_location") or "").strip(),
    }


def _by_address(address: str) -> dict | None:
    """Assessor addresses look like `0000 0650 ELLIS  ST0000` — a range prefix,
    a zero-padded number, then a padded street name. Padding the house number
    to four digits is what stops `650 ELLIS` from also matching `1650 ELLIS`.
    """
    m = re.match(r"\s*(\d+)\s+(.+)", address or "")
    if not m:
        return None
    number, street = m.group(1), m.group(2).upper()
    # Drop unit designators and the street type; the assessor stores its own.
    street = re.split(r"\b(?:APT|UNIT|STE|#)\b", street)[0]
    street = re.sub(r"\b(ST|STREET|AVE|AVENUE|BLVD|BOULEVARD|RD|ROAD|DR|DRIVE|WAY|PL|PLACE|CT|TER|TERRACE|LN|LANE)\b\.?", "", street)
    street = re.sub(r"[^A-Z0-9 ]", " ", street).strip()
    if not street:
        return None
    needle = f"{int(number):04d} {street.split()[0]}"
    rows = _query(f"property_location like '%{needle}%'")
    for row in rows:
        v = _verdict(row)
        if v:
            return v
    return None


def _by_coords(lat: float, lon: float, radius_m: int = 35) -> dict | None:
    rows = _query(f"within_circle(the_geom, {lat}, {lon}, {radius_m})", limit=10)
    best = None
    for row in rows:
        v = _verdict(row)
        if not v:
            continue
        # Prefer the biggest multi-unit building near the pin: a rental listing
        # is far likelier to be the 30-unit block than the corner shop.
        if best is None or v["units"] > best["units"]:
            best = v
    return best


def lookup(store, listing) -> dict | None:
    """Cached rent-control verdict for a listing, or None if unknown."""
    address = (listing.address or "").strip()
    key = None
    if re.match(r"\s*\d+\s+\S", address):
        key = f"addr:{address.upper()[:120]}"
    elif listing.lat is not None and listing.lon is not None:
        key = f"geo:{listing.lat:.5f},{listing.lon:.5f}"
    if key is None:
        return None

    cached = store.building_info(key)
    if cached is not None:
        return cached or None  # {} means "looked up, nothing found"

    info = _by_address(address) if key.startswith("addr:") else None
    if info is None and listing.lat is not None and listing.lon is not None:
        info = _by_coords(listing.lat, listing.lon)

    # Cache the miss too, so a building with no assessor record isn't re-queried
    # on every run forever.
    store.save_building_info(key, info or {})
    return info


def label(info: dict | None) -> str:
    """Short digest marker. '?' because this is a heuristic, not a determination."""
    if not info:
        return ""
    if info.get("controlled"):
        return f" RC?{info['year_built']}"
    return ""
