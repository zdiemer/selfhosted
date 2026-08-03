"""Which SF neighborhood is this listing in?

Two ways to answer, in order of trust:

1. Point-in-polygon on the listing's lat/lon against SF Find Neighborhoods
   (DataSF `gfpk-269f`, baked into data/sf-neighborhoods.geojson). This is the
   real answer — it doesn't care what the listing *claims*.
2. Fuzzy-match whatever neighborhood string the source printed, for listings
   with no coordinates.

Craigslist posters routinely mislabel neighborhoods, sometimes deliberately
("Potrero Hill" on a Bayview unit reads better), so the coordinate path is the
one that matters and the string path is a fallback, never an override.

No shapely: ray casting is short, exact enough for neighborhood boundaries, and
keeps GEOS out of the image.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

_DATA = os.environ.get(
    "APARTMENT_WATCH_GEOJSON",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sf-neighborhoods.geojson"),
)

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
}


@lru_cache(maxsize=1)
def _neighborhoods() -> list[tuple[str, list[list[tuple[float, float]]], tuple[float, float, float, float]]]:
    """(name, rings, bbox) per neighborhood, with bboxes precomputed.

    Flattens MultiPolygon rings into one list per neighborhood. SF's polygons
    don't overlap and none of them have holes that matter at this scale, so
    treating every ring as an outer ring is fine and keeps the test simple.
    """
    with open(_DATA) as fh:
        data = json.load(fh)

    out = []
    for feat in data["features"]:
        name = feat["properties"]["name"]
        geom = feat["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]

        rings: list[list[tuple[float, float]]] = []
        for poly in polys:
            for ring in poly:
                rings.append([(float(x), float(y)) for x, y in ring])

        xs = [x for r in rings for x, _ in r]
        ys = [y for r in rings for _, y in r]
        out.append((name, rings, (min(xs), min(ys), max(xs), max(ys))))
    return out


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


def neighborhood_for(lat: float | None, lon: float | None) -> str | None:
    """The neighborhood containing this point, or None if outside SF."""
    if lat is None or lon is None:
        return None
    for name, rings, (minx, miny, maxx, maxy) in _neighborhoods():
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            continue
        if any(_in_ring(lon, lat, ring) for ring in rings):
            return name
    return None


@lru_cache(maxsize=512)
def normalize_name(raw: str | None) -> str | None:
    """Best-effort map of a free-text neighborhood string onto a polygon name.

    Only used when a listing has no coordinates. Returns None rather than
    guessing wildly — an unknown neighborhood is not an excluded one.
    """
    if not raw:
        return None
    text = re.sub(r"[^a-z' ]+", " ", raw.lower()).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None

    if text in ALIASES:
        return ALIASES[text]

    known = {n.lower(): n for n, _, _ in _neighborhoods()}
    if text in known:
        return known[text]

    # Substring both ways: "nob hill apartment" -> Nob Hill, and a source that
    # says "Bayview" when the polygon is "Bayview" already matched above.
    for alias, canonical in ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return canonical
    for lowered, canonical in known.items():
        if re.search(rf"\b{re.escape(lowered)}\b", text):
            return canonical
    return None


def resolve(
    lat: float | None, lon: float | None, stated: str | None
) -> tuple[str | None, str]:
    """Return (neighborhood, how) where `how` is 'coords', 'stated', or 'unknown'.

    Callers surface `how` in logs so a run that silently fell back to poster-
    supplied names everywhere is visible rather than assumed accurate.
    """
    by_coords = neighborhood_for(lat, lon)
    if by_coords:
        return by_coords, "coords"
    by_name = normalize_name(stated)
    if by_name:
        return by_name, "stated"
    return None, "unknown"
