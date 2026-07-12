#!/usr/bin/env python3
"""Fill Wii and GameCube wraps from GameTDB — the free, un-gated source.

Cover Project is the big archive but it's behind a Cloudflare managed challenge, and
its full coverage of Wii/GC is patchy. GameTDB has a real full wrap (back|spine|front)
for ~96-100% of Wii and GameCube games, at URLs you CONSTRUCT from the game's ID — no
crawl, no Cloudflare, no opaque keys:

    https://art.gametdb.com/wii/coverfullHQ/<REGION>/<ID>.png    (GC lives under /wii/ too)

The only work is title -> ID, and GameTDB hands us that too: the tdb text database is a
plain "<ID> = <Title>" list, downloadable, no auth. The 4th character of the ID is the
region (E=US, P=EU/PAL, J=JP), and the region FOLDER is a function of it — E->US, but
P->EN (not "EU"), verified against real 404s.

This ONLY covers what GameTDB actually has as a flat wrap: Wii and GameCube. Its DS,
3DS and Wii U art is a front-only 3D box render, not a wrap, so those platforms are not
touched here.

It runs AFTER resolve_covers.py and only fills games Cover Project didn't already cover
— a real box scan we sliced ourselves beats a constructed URL, but a constructed URL
beats an IGDB front.

Usage:  python3 tools/gametdb_covers.py --api https://games.zachd.duckdns.org
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

ART = "https://art.gametdb.com/wii/coverfullHQ/{region}/{id}.png"
# Both Wii and GameCube IDs live in wiitdb.txt (there is no usable gctdb); GC IDs are
# the ones whose first letter is G.
TDB = "https://www.gametdb.com/wiitdb.txt?LANG=EN"

# The sheet's platform -> which ID prefix we accept. GameCube IDs start with G or D;
# Wii IDs start with R or S (and a few others). We don't hard-filter on the letter —
# we match by title and trust the database — but we DO use it to keep a GameCube game
# from grabbing a Wii ID of the same name (e.g. a game released on both).
PLATFORM = {
    "Nintendo GameCube": "gc", "GameCube": "gc",
    "Nintendo Wii": "wii", "Wii": "wii",
}

# 4th char of the ID -> the CDN's region folder. Note P maps to EN, not EU.
REGION_FOLDER = {"E": "US", "P": "EN", "J": "JP", "K": "KO"}
# Which region to prefer, given the sheet's release region.
REGION_PREF = {
    "North America": ["E", "P", "J"],
    "Europe": ["P", "E", "J"],
    "Japan": ["J", "E", "P"],
}
DEFAULT_PREF = ["E", "P", "J"]

# GameTDB's Wii/GC wrap is a standard DVD-keepcase layout; the case dims differ by
# platform but the slice ratio is the same ~1.51. (Kept in step with cp_wrap.TEMPLATES.)
TEMPLATE = {"wii": "dvd", "gc": "gc"}


def norm(title: str) -> str:
    """Loose title key for matching the sheet against GameTDB."""
    s = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    return s


def get(url: str, timeout: int = 60) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gamedex/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def exists(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "gamedex/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def load_index() -> dict[str, dict[str, str]]:
    """norm(title) -> {region_char: id}. Later duplicate titles don't clobber earlier
    region variants, so a game present in US and EU keeps both."""
    raw = get(TDB, 120)
    if not raw:
        print("could not fetch the GameTDB database", file=sys.stderr)
        return {}
    idx: dict[str, dict[str, str]] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        m = re.match(r"^([A-Z0-9]{4,6})\s*=\s*(.+)$", line.strip())
        if not m:
            continue
        gid, title = m.group(1), m.group(2).strip()
        if len(gid) < 4:
            continue
        region = gid[3]
        if region not in REGION_FOLDER:
            continue
        idx.setdefault(norm(title), {}).setdefault(region, gid)
    return idx


def resolve(game, index) -> dict | None:
    plat = PLATFORM.get(game.get("platform"))
    if not plat:
        return None
    variants = index.get(norm(game.get("title")))
    if not variants:
        return None
    # GameCube IDs start with G/D, Wii with R/S/etc. Keep them from crossing.
    def ok(gid):
        first = gid[0]
        return (first in "GD") if plat == "gc" else (first not in "GD")
    variants = {r: gid for r, gid in variants.items() if ok(gid)}
    if not variants:
        return None

    for region in REGION_PREF.get(game.get("releaseRegion") or "", DEFAULT_PREF):
        gid = variants.get(region)
        if not gid:
            continue
        folder = REGION_FOLDER[region]
        url = ART.format(region=folder, id=gid)
        if exists(url):
            return {"url": url, "region": folder, "rot": 0,
                    "template": TEMPLATE[plat], "source": "gametdb"}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="https://games.zachd.duckdns.org")
    ap.add_argument("--resolved", default="data/covers-resolved.json")
    a = ap.parse_args()

    resolved = json.loads(pathlib.Path(a.resolved).read_text())
    wraps = resolved.setdefault("wraps", {})
    hues = resolved.setdefault("hues", {})

    print("loading the collection and the GameTDB database ...", file=sys.stderr)
    rows = json.loads(get(f"{a.api}/api/data", 300))["sheets"]["games"]["rows"]
    index = load_index()
    print(f"  {len(index)} GameTDB titles", file=sys.stderr)

    phys = [r for r in rows if r.get("owned")
            and (r.get("format") or "").strip().lower() in ("physical", "both")
            and r.get("_k") and r.get("platform") in PLATFORM]

    added = 0
    for r in phys:
        key = f"{r['_k']}#{(r.get('releaseRegion') or '').strip()}"
        if key in wraps:                        # Cover Project already has a real scan
            continue
        hit = resolve(r, index)
        if not hit:
            continue
        # The wrap decides the box; use the platform's real case dims.
        from cp_wrap import TEMPLATES  # noqa: E402
        back, spine, front, h = TEMPLATES[hit["template"]]
        hit["case"] = {"w": front, "h": h, "d": spine}
        wraps[key] = hit
        added += 1
        if added % 10 == 0:
            print(f"  +{added} GameTDB wraps", file=sys.stderr)

    pathlib.Path(a.resolved).write_text(json.dumps(resolved, separators=(",", ":")))
    n_g = sum(1 for v in wraps.values() if v.get("source") == "gametdb")
    print(f"\nadded {added} GameTDB wraps (Wii/GC); {n_g} total from GameTDB, "
          f"{len(wraps)} wraps overall", file=sys.stderr)


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    main()
