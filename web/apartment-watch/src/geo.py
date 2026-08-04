"""Where is this listing, and is that somewhere we're looking?

Two ways to answer, in order of trust:

1. Point-in-polygon on the listing's lat/lon. This is the real answer — it
   doesn't care what the listing *claims*.
2. Fuzzy-match whatever neighborhood or city string the source printed, for
   listings with no coordinates.

Craigslist posters routinely mislabel neighborhoods, sometimes deliberately
("Potrero Hill" on a Bayview unit reads better), so the coordinate path is the
one that matters and the string path is a fallback, never an override.

Two polygon files, both baked into data/:

* **sf-neighborhoods.geojson** — SF Find Neighborhoods (DataSF `gfpk-269f`),
  117 polygons. Every feature is region `san_francisco`.
* **search-areas.geojson** — the towns outside SF, from US Census TIGERweb.
  Each feature carries its own `region`, and a `role`:
  `place` is a town, `fill` is a wider polygon (Marin County) clipped by a
  `bounds` box. The fill exists because incorporated towns don't tile a county:
  Greenbrae, Bon Air and the edges of Ross Valley sit in unincorporated gaps
  between them, and without it a perfectly good Greenbrae listing resolves to
  nowhere and gets dropped as "outside search area". Places are matched first,
  so the fill only ever names somewhere no town claimed.

No shapely: ray casting is short, exact enough for these boundaries, and keeps
GEOS out of the image.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_SF_DATA = os.environ.get(
    "APARTMENT_WATCH_GEOJSON", os.path.join(_DIR, "sf-neighborhoods.geojson")
)
_AREA_DATA = os.environ.get(
    "APARTMENT_WATCH_AREAS_GEOJSON", os.path.join(_DIR, "search-areas.geojson")
)

SF_REGION = "san_francisco"

# Mean radius in miles. Distances here are a few miles across one city, where
# the spherical error is centimetres.
_EARTH_MILES = 3958.7613

# Names people actually type -> the SF Find Neighborhoods polygon name. Only
# needed for the no-coordinates fallback path.
ALIASES = {
    "tenderloin": "Tenderloin",
    "tendernob": "Tenderloin",
    "the tenderloin": "Tenderloin",
    "bayview": "Bayview",
    "bay view": "Bayview",
    "bayview district": "Bayview",
    "bayview-hunters point": "Bayview",
    "bayview hunters point": "Bayview",
    "hunters point": "Hunters Point",
    "hunter's point": "Hunters Point",
    "hunterspoint": "Hunters Point",
    "potrero": "Potrero Hill",
    "potrero hill": "Potrero Hill",
    "soma": "South of Market",
    "south of market": "South of Market",
    "noe": "Noe Valley",
    "nopa": "Western Addition",
    "lower haight": "Lower Haight",
    "inner richmond": "Inner Richmond",
    "outer richmond": "Outer Richmond",
    "inner sunset": "Inner Sunset",
    "outer sunset": "Outer Sunset",
    "nob hill": "Nob Hill",
    "russian hill": "Russian Hill",
    "north beach": "North Beach",
    "hayes valley": "Hayes Valley",
    "mission": "Mission",
    "the mission": "Mission",
    "mission district": "Mission",
    "marina": "Marina",
    "marina district": "Marina",
    "pac heights": "Pacific Heights",
    "pacific heights": "Pacific Heights",
    # The towns outside SF are matched by their own names; these are the loose
    # ways a poster refers to the area as a whole.
    "marin": "Marin",
    "marin county": "Marin",
    # Unincorporated, so it has no polygon of its own — it sits in the gap
    # between Larkspur and Kentfield that the Marin fill covers. Naming it
    # "Marin" is vaguer than the truth but never wrong, where mapping it onto a
    # neighbouring town would print a place it isn't in.
    "greenbrae": "Marin",
    "bon air": "Marin",
    "southern marin": "Marin",
    "south marin": "Marin",
    "tam valley": "Tamalpais-Homestead Valley",
    "tamalpais valley": "Tamalpais-Homestead Valley",
    "homestead valley": "Tamalpais-Homestead Valley",
    "belvedere tiburon": "Tiburon",
}


@dataclass(frozen=True)
class Region:
    name: str
    region: str
    role: str                                  # "place" or "fill"
    rings: tuple[tuple[tuple[float, float], ...], ...]
    bbox: tuple[float, float, float, float]    # minlon, minlat, maxlon, maxlat
    # Extra clip for a fill polygon: inside the polygon AND inside this box.
    bounds: tuple[float, float, float, float] | None = None

    def contains(self, lat: float, lon: float) -> bool:
        minx, miny, maxx, maxy = self.bbox
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            return False
        if self.bounds:
            bx0, by0, bx1, by1 = self.bounds
            if not (bx0 <= lon <= bx1 and by0 <= lat <= by1):
                return False
        return any(_in_ring(lon, lat, ring) for ring in self.rings)


def _features(path: str, default_region: str) -> list[Region]:
    """Flatten one GeoJSON file into Region records with bboxes precomputed.

    MultiPolygon rings collapse into one list per feature: these polygons don't
    overlap and none of them have holes that matter at this scale, so treating
    every ring as an outer ring is fine and keeps the test simple.
    """
    with open(path) as fh:
        data = json.load(fh)

    out: list[Region] = []
    for feat in data["features"]:
        props = feat["properties"]
        geom = feat["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]

        rings = tuple(
            tuple((float(x), float(y)) for x, y in ring)
            for poly in polys for ring in poly
        )
        xs = [x for r in rings for x, _ in r]
        ys = [y for r in rings for _, y in r]
        bounds = props.get("bounds")
        out.append(Region(
            name=props["name"],
            region=props.get("region", default_region),
            role=props.get("role", "place"),
            rings=rings,
            bbox=(min(xs), min(ys), max(xs), max(ys)),
            bounds=tuple(bounds) if bounds else None,
        ))
    return out


@lru_cache(maxsize=1)
def _regions() -> list[Region]:
    """Every polygon we know, places before fills.

    Order is the containment rule: a point inside both Kentfield and the Marin
    fill is Kentfield.
    """
    regions = _features(_SF_DATA, SF_REGION)
    if os.path.exists(_AREA_DATA):
        regions += _features(_AREA_DATA, SF_REGION)
    else:
        logger.warning("no %s — searching San Francisco only", _AREA_DATA)
    return sorted(regions, key=lambda r: r.role != "place")


def _in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Standard even-odd ray cast, counting crossings of a ray heading east."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        # Does the edge straddle the horizontal line through our point?
        if (y1 > lat) != (y2 > lat):
            # x of the edge at y=lat. y2 != y1 is guaranteed by the check above.
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if x_at > lon:
                inside = not inside
    return inside


def place_for(lat: float | None, lon: float | None) -> tuple[str | None, str | None]:
    """(name, region) of the polygon containing this point, or (None, None)."""
    if lat is None or lon is None:
        return None, None
    for entry in _regions():
        if entry.contains(lat, lon):
            return entry.name, entry.region
    return None, None


def neighborhood_for(lat: float | None, lon: float | None) -> str | None:
    """The place containing this point, or None if it's outside every area."""
    return place_for(lat, lon)[0]


@lru_cache(maxsize=512)
def normalize_name(raw: str | None) -> tuple[str, str] | None:
    """Best-effort map of a free-text place string onto (polygon name, region).

    Only used when a listing has no coordinates. Returns None rather than
    guessing wildly — an unknown place is not an excluded one.
    """
    if not raw:
        return None
    text = re.sub(r"[^a-z' ]+", " ", raw.lower()).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None

    known = {e.name.lower(): (e.name, e.region) for e in _regions()}

    if text in ALIASES:
        return known.get(ALIASES[text].lower())
    if text in known:
        return known[text]

    # Substring both ways: "nob hill apartment" -> Nob Hill, and a source that
    # says "Bayview" when the polygon is "Bayview" already matched above.
    for alias, canonical in ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            hit = known.get(canonical.lower())
            if hit:
                return hit
    for lowered, hit in known.items():
        if re.search(rf"\b{re.escape(lowered)}\b", text):
            return hit
    return None


def resolve(
    lat: float | None, lon: float | None, stated: str | None
) -> tuple[str | None, str | None, str]:
    """Return (place, region, how) — `how` is 'coords', 'stated', or 'unknown'.

    Callers surface `how` in logs so a run that silently fell back to poster-
    supplied names everywhere is visible rather than assumed accurate.
    """
    name, region = place_for(lat, lon)
    if name:
        return name, region, "coords"
    by_name = normalize_name(stated)
    if by_name:
        return by_name[0], by_name[1], "stated"
    return None, None, "unknown"


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line miles. Haversine, not driving distance.

    Driving distance would need a routing API and a key for a threshold that is
    a judgement call to start with ("about three miles"), so the extra
    dependency would buy precision the rule doesn't have.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_MILES * math.asin(math.sqrt(a))


def miles_from(
    lat: float | None, lon: float | None, reference: tuple[float, float] | None
) -> float | None:
    """Distance to a reference point, or None if either end is unknown.

    None means "we couldn't place it", which callers must treat differently
    from "it's close" — see the parking rule in main.evaluate.
    """
    if lat is None or lon is None or reference is None:
        return None
    return miles_between(lat, lon, reference[0], reference[1])
