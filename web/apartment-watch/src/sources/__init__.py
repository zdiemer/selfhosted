"""Source registry.

Adding a site means writing one module with a `search(fetcher, criteria, seen)`
generator and adding it here. Nothing else in the codebase needs to change —
matching, scam scoring, dedup, and the digest are all source-agnostic.
"""

from __future__ import annotations

from .apartments_com import ApartmentsCom
from .craigslist import Craigslist
from .zillow import Zillow
from .zumper import Zumper

REGISTRY = {
    Craigslist.name: Craigslist,
    Zumper.name: Zumper,
    ApartmentsCom.name: ApartmentsCom,
    Zillow.name: Zillow,
}


def build(name: str):
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"unknown source {name!r}; known: {sorted(REGISTRY)}")
    return cls()
