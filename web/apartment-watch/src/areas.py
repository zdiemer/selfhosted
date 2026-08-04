"""Where we search, and how each site spells it.

criteria.yaml names areas (`search.areas: [san_francisco, southern_marin, ...]`)
and nothing else. Everything below is site plumbing — Craigslist subarea codes,
URL slugs, the city strings each site prints — which is not a tuning surface and
would only be one more thing to get wrong in YAML.

Two independent filters keep results inside an area, and they are meant to
overlap:

* **Source-side**, per site: Craigslist's subarea plus a city-slug prefix,
  Zumper's and Apartments.com's `city` / `addressLocality` fields. Cheap, and it
  runs before we spend a detail fetch.
* **Coordinates**, in main.evaluate, against the polygons in data/. This is the
  authority — a source-side filter is a first pass, not a guarantee.

`sf_records` is the one that bites if you get it wrong: rent control and the
address-beats-pin geocode both query the *San Francisco* assessor, and a
Burlingame address like "1200 Broadway" will happily match San Francisco's 1200
Broadway and move the listing four bay-miles north. Only San Francisco may ask.
"""

from __future__ import annotations

from dataclasses import dataclass

SF = "san_francisco"


@dataclass(frozen=True)
class Area:
    key: str
    label: str
    # Craigslist subarea of sfbay, and the city slugs a post URL may start with.
    # `sfbay.craigslist.org/search/nby/apa` really is constrained to the North
    # Bay, but the North Bay is mostly Sonoma and Napa, so the slug list is what
    # narrows it to the part we want.
    cl_subarea: str
    cl_slugs: tuple[str, ...]
    # City strings as these sites print them, lowercased. Their area pages are
    # wider than the area (the SF page carries Daly City; the Marin page carries
    # Novato), so every row is checked against this.
    cities: tuple[str, ...]
    # Path segment on each site, or None to skip that site for this area.
    zumper_path: str | None = None
    apartments_path: str | None = None
    zillow_path: str | None = None
    # May we ask the San Francisco assessor about addresses here?
    sf_records: bool = False


_MARIN_CITIES = (
    "belvedere", "corte madera", "fairfax", "greenbrae", "kentfield", "larkspur",
    "marin city", "mill valley", "ross", "san anselmo", "san rafael",
    "santa venetia", "sausalito", "sleepy hollow", "strawberry",
    "tamalpais-homestead valley", "tamalpais valley", "tiburon",
)

AREAS: dict[str, Area] = {
    SF: Area(
        key=SF,
        label="San Francisco",
        cl_subarea="sfc",
        cl_slugs=("san-francisco",),
        cities=("san francisco",),
        zumper_path="san-francisco-ca",
        apartments_path="san-francisco-ca",
        zillow_path="san-francisco-ca",
        sf_records=True,
    ),
    "southern_marin": Area(
        key="southern_marin",
        label="Southern Marin",
        # nby is the whole North Bay — Santa Rosa alone was 166 of 348 results
        # in a live pull — so the slug list is doing the real work here.
        cl_subarea="nby",
        cl_slugs=(
            "belvedere", "belvedere-tiburon", "corte-madera", "fairfax", "greenbrae",
            "kentfield", "larkspur", "marin-city", "mill-valley", "ross",
            "san-anselmo", "san-rafael", "santa-venetia", "sausalito",
            "sleepy-hollow", "strawberry", "tamalpais-valley", "tiburon",
        ),
        cities=_MARIN_CITIES,
        zumper_path="marin-county-ca",
        apartments_path="marin-county-ca",
        # Zillow stays San Francisco only: it has produced zero matches in every
        # live run, its detail pages still 403, and it is the most expensive
        # source per listing. Give it a path here if that ever changes.
        zillow_path=None,
    ),
    "burlingame": Area(
        key="burlingame",
        label="Burlingame",
        cl_subarea="pen",
        cl_slugs=("burlingame",),
        cities=("burlingame",),
        zumper_path="burlingame-ca",
        apartments_path="burlingame-ca",
        zillow_path=None,
    ),
}


def get(key: str) -> Area:
    return AREAS[key]


def enabled(criteria) -> list[Area]:
    """The areas this run searches, in criteria.yaml order."""
    return [AREAS[key] for key in criteria.search.areas]


def for_source(criteria, attr: str) -> list[Area]:
    """Enabled areas this source can actually search (i.e. has a path for)."""
    return [a for a in enabled(criteria) if getattr(a, attr)]


def matches_slug(area: Area, slug: str) -> bool:
    """Is this Craigslist post URL slug in `area`?

    Post slugs are `<city>-<title-words>`, e.g. `mill-valley-sunny-1br-…`, so a
    prefix test needs the trailing dash — without it `ross-` would also swallow
    `rossmoor-`.
    """
    return any(slug == city or slug.startswith(city + "-") for city in area.cl_slugs)


def in_area(area: Area, city: str | None) -> bool:
    """Does a site-printed city string belong to `area`? Blank means 'unstated',
    which is left to the coordinate check rather than dropped here."""
    text = (city or "").strip().lower()
    return not text or text in area.cities
