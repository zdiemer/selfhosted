"""Wikidata — the bridge, and the credits nobody else carries.

Wikidata is not a games database and shouldn't be used as one. What it IS, uniquely, is a
free bulk crosswalk: 146,914 items carry an IGDB id (P5794), and hanging off those same
items are the identifiers and facts no source in this app has ever had — a MobyGames id, a
Wikipedia article, the composer, the director.

The join is on the IGDB **slug**, not the numeric id: P5794 stores `chrono-trigger`, and
every one of our IGDB records already carries the same slug in its `url`. So this costs no
matching and no confidence score — either Wikidata knows the slug or it doesn't.

Fetched in FOUR queries rather than one. A single query with four OPTIONALs over 147k items
is the obvious way to write it and it times out; asking four narrow questions and merging
the answers locally takes about 15 seconds in total. Measured:

    igdb -> mobygames    33,902 rows    2.2s
    igdb -> en.wikipedia 29,978 rows    8.6s
    igdb -> composer      4,987 rows    2.6s
    igdb -> director      1,069 rows    1.2s

Cached on the PVC. There are no per-game requests at all: a lookup is a dict hit.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import threading
import time

import requests

log = logging.getLogger("gamedex.wikidata")

SPARQL = "https://query.wikidata.org/sparql"
_UA = "gamedex/1.0 (personal game collection; +https://github.com/zdiemer)"

MOBY = "https://www.mobygames.com/game/{}"

# One narrow query per fact. Keys are the field they populate; `multi` means a game can
# have several (two composers is normal; two MobyGames ids is not).
_QUERIES = {
    "moby": ("""SELECT ?igdb ?v WHERE { ?i wdt:P5794 ?igdb; wdt:P1933 ?v }""", False),
    "wikipedia": ("""SELECT ?igdb ?v WHERE {
        ?i wdt:P5794 ?igdb .
        ?v schema:about ?i; schema:isPartOf <https://en.wikipedia.org/> }""", False),
    "composers": ("""SELECT ?igdb ?v WHERE {
        ?i wdt:P5794 ?igdb; wdt:P86 ?c . ?c rdfs:label ?v . FILTER(lang(?v)="en") }""", True),
    "directors": ("""SELECT ?igdb ?v WHERE {
        ?i wdt:P5794 ?igdb; wdt:P57 ?d . ?d rdfs:label ?v . FILTER(lang(?v)="en") }""", True),
}
_MAX_MULTI = 4          # four composers is a soundtrack credit list, not a fact


def slug_from_igdb_url(url: str | None) -> str | None:
    """`https://www.igdb.com/games/chrono-trigger` -> `chrono-trigger`."""
    m = re.search(r"igdb\.com/games/([^/?#]+)", (url or "").strip())
    return m.group(1).lower() if m else None


class Wikidata:
    def __init__(self, cache_dir: str = "/data/wikidata", ttl_days: int = 30):
        self._dir = pathlib.Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "wikidata.json"
        self._ttl = ttl_days * 86400
        self._map: dict[str, dict] = {}      # igdb slug -> record
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._s.headers["Accept"] = "application/sparql-results+json"
        self._load()

    @property
    def ready(self) -> bool:
        return bool(self._map)

    def serves(self, platform: str | None) -> bool:
        return True                      # a game is a game; the slug is the only gate

    # -- the dump ------------------------------------------------------------
    def _load(self) -> None:
        try:
            blob = json.loads(self._path.read_text())
            if time.time() - blob.get("fetched", 0) < self._ttl and blob.get("version") == 1:
                self._map = blob["games"]
                log.info("wikidata: %d IGDB slugs from cache", len(self._map))
                return
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("wikidata: cache unreadable (%s)", exc)
        # ~20s of SPARQL. Not enough to time out a rollout, but there is no reason to make
        # the pod wait on it either — and `ready` gives the enricher something honest to
        # check, so a game asked about too early is requeued, not written off as no_match.
        threading.Thread(target=self.refresh, name="wikidata-dump", daemon=True).start()

    def _run(self, query: str) -> list[tuple[str, str]]:
        r = self._s.get(SPARQL, params={"query": query}, timeout=120)
        r.raise_for_status()
        out = []
        for b in r.json()["results"]["bindings"]:
            slug = (b.get("igdb", {}).get("value") or "").lower()
            val = b.get("v", {}).get("value")
            if slug and val:
                out.append((slug, val))
        return out

    def refresh(self) -> None:
        games: dict[str, dict] = {}
        got = 0
        for field, (query, multi) in _QUERIES.items():
            try:
                rows = self._run(query)
            except Exception as exc:
                # One slice failing is not fatal — the others still carry their facts.
                log.warning("wikidata: %s query failed (%s)", field, exc)
                continue
            for slug, val in rows:
                rec = games.setdefault(slug, {})
                if multi:
                    vals = rec.setdefault(field, [])
                    if val not in vals and len(vals) < _MAX_MULTI:
                        vals.append(val)
                else:
                    rec.setdefault(field, val)
            got += len(rows)
            log.info("wikidata: %s -> %d rows", field, len(rows))
        if not games:
            return                       # keep a stale map rather than wipe it
        self._map = games
        try:
            self._path.write_text(json.dumps(
                {"fetched": time.time(), "version": 1, "games": games}))
        except Exception as exc:
            log.warning("wikidata: could not write cache (%s)", exc)
        log.info("wikidata: %d slugs indexed from %d rows", len(games), got)

    # -- lookup --------------------------------------------------------------
    def _record(self, slug: str, raw: dict) -> dict:
        moby = raw.get("moby")
        return {
            "source": "Wikidata",
            "slug": slug,
            "mobyId": moby,
            "mobyUrl": MOBY.format(moby) if moby else None,
            "wikipedia": raw.get("wikipedia"),
            "composers": raw.get("composers") or [],
            "directors": raw.get("directors") or [],
            # Keyed on the IGDB slug, so like PCGamingWiki this is an exact join and not a
            # title guess. It cannot be the wrong game.
            "confidence": 15,
        }

    def match_meta(self, meta: dict):
        """Exact lookup on the IGDB slug the enricher hands us. No network, no matching."""
        slug = (meta.get("igdbSlug") or "").lower()
        if not slug or not self._map:
            return None
        raw = self._map.get(slug)
        if not raw:
            return None
        rec = self._record(slug, raw)
        # A row with an IGDB slug and nothing hanging off it is not worth storing.
        if not (rec["mobyUrl"] or rec["wikipedia"] or rec["composers"] or rec["directors"]):
            return None
        return rec

    def match(self, title: str, platform=None, year=None):
        return None                      # slug-keyed; the entry point is match_meta

    def override_from_url(self, title: str, url: str):
        """Paste the game's IGDB URL to re-pin which Wikidata row it maps to."""
        slug = slug_from_igdb_url(url)
        return self.match_meta({"igdbSlug": slug}) if slug else None
