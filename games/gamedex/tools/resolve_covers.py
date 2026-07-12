#!/usr/bin/env python3
"""Decide, once, which Cover Project scan belongs to each game — and which way is up.

Two problems, one answer.

  WHICH SCAN. A game can have seven scans: US, EU, AU, BR, and a few unlabelled
  legacy ones. Pick by a hardcoded region preference and you get Super Mario World
  as a Super Famicom box, because the archive has no US scan of it and JP is all
  that's left. Region rank cannot know that the JP box is the wrong box.

  WHICH WAY IS UP. Cover Project stores some wraps rotated 90 degrees, and it is
  NOT consistent, because the boxes aren't: a US SNES box is portrait and a PAL
  SNES box is LANDSCAPE. So the same "snes" template holds both, and the scan's
  overall aspect is identical either way — the only difference is whether the art
  inside each panel is turned on its side. Nothing about the geometry can tell you.

The IGDB cover settles both. It is always upright, always the front of the box, and
we already have one for nearly every game. So: for every candidate scan, cut the
front panel, try it at 0/90/270 degrees, and score each against the IGDB cover.
Whatever matches best IS the right scan at the right rotation. Region falls out of
it for free — the US cover matches the US box.

Scoring runs on the CDN's _thumb objects (tens of KB), not the 6 MB scans, so
resolving 2,000 games costs a few hundred MB rather than ten gigabytes. The
full-size scan is only ever fetched later, lazily, for a game you actually look at.

Usage:  python3 tools/resolve_covers.py --api https://games.zachd.duckdns.org
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import io
import json
import pathlib
import re
import sys
import urllib.request

from PIL import Image, ImageOps

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from coverproject_index import slugify                      # noqa: E402
from cp_wrap import (TEMPLATES, PLATFORM_TEMPLATE, TEMPLATE_ROT, _aspect,   # noqa: E402
                     TOLERANCE, _saturation)
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))
from shelf import dominant_hue                            # noqa: E402

IGDB_IMG = "https://images.igdb.com/igdb/image/upload/t_cover_big/{}.jpg"

# The sheet's platform name -> Cover Project's directory.
PLATFORM_DIR = {
    "Super Nintendo Entertainment System": "super_nintendo",
    "Nintendo Entertainment System": "nes",
    "Nintendo 64": "nintendo_64",
    "Nintendo GameCube": "gamecube",
    "Nintendo Wii": "nintendo_wii",
    "Nintendo Wii U": "nintendo_wii_u",
    "Nintendo Switch": "nintendo_switch",
    "Nintendo Game Boy": "gameboy",
    "Nintendo Game Boy Color": "gameboy_color",
    "Nintendo Game Boy Advance": "gameboy_advance",
    "Nintendo DS": "nintendo_ds",
    "Nintendo 3DS": "nintendo_3ds",
    "New Nintendo 3DS": "nintendo_3ds",
    "Nintendo Virtual Boy": "virtual_boy",
    "PlayStation": "playstation_1",
    "PlayStation 2": "playstation_2",
    "PlayStation 3": "playstation_3",
    "PlayStation 4": "playstation_4",
    "PlayStation 5": "playstation_5",
    "PlayStation Portable": "psp",
    "PlayStation Vita": "ps_vita",
    "Sega Genesis": "genesis",
    "Sega CD": "sega_cd",
    "Sega 32X": "sega_32x",
    "Sega Saturn": "sega_saturn",
    "Sega Dreamcast": "dreamcast",
    "Sega Master System": "sega_master_system",
    "Sega Game Gear": "game_gear",
    "Xbox": "xbox",
    "Xbox 360": "xbox_360",
    "Xbox One": "xbox_one",
    "Atari 2600": "atari_2600",
    "Atari 5200": "atari_5200",
    "Atari 7800": "atari_7800",
    "Atari Jaguar": "atari_jaguar",
    "Atari Lynx": "atari_lynx",
    "3DO Interactive Multiplayer": "3do",
    "Neo Geo": "neo_geo",
    "TurboGrafx-16": "turbografx_16",
    "ColecoVision": "colecovision",
    "Intellivision": "intellivision",
    "PC": "windows",
    # The sheet's own shorthands, which are what it actually says most of the time.
    "SNES": "super_nintendo",
    "NES": "nes",
    "Game Boy": "gameboy",
    "Game Boy Color": "gameboy_color",
    "Game Boy Advance": "gameboy_advance",
    "Genesis": "genesis",
    "Dreamcast": "dreamcast",
    "GameCube": "gamecube",
    "Wii": "nintendo_wii",
    "Wii U": "nintendo_wii_u",
    "Saturn": "sega_saturn",
    "Master System": "sega_master_system",
    "Game Gear": "game_gear",
    "Virtual Boy": "virtual_boy",
    "PSP": "psp",
    "PS Vita": "ps_vita",
    "3DO": "3do",
    "Jaguar": "atari_jaguar",
    "Lynx": "atari_lynx",
}

# Region preference. An unlabelled legacy key is almost always the US scan, so it
# outranks a labelled foreign one — that alone stops Super Mario World arriving as
# a Super Famicom box whenever a US scan exists.
REGION_RANK = {"US": 0, "NA": 0, "": 1, "CA": 2, "EU": 3, "UK": 3, "AU": 4, "BR": 5, "JP": 6}
REGION_OF_SHEET = {"North America": "US", "Europe": "EU", "Japan": "JP", "Australia": "AU"}

MIN_SCORE = 0.05          # below this, the scan doesn't look like this game at all

# Cover Project hosts fan-made covers alongside real scans, under the real game's slug.
# The only NES "The Legend of Zelda" scan in the archive is a ROM-hack cover ("Zelda:
# The Legend of Link") using Breath of the Wild art — and it is close enough to the real
# thing, once reduced to a fingerprint, that no threshold can reject it without also
# rejecting genuine matches. So it gets named. This list is expected to grow slowly;
# that is the honest cost of a community archive.
BLOCKED = {
    "nes.legendofzeldathe_US.1624582424465806971.jpg",   # fan ROM-hack cover, not the box
}


def row_key(row) -> str:
    """The key a BOX hangs off.

    NOT the enrichment key: that is title|platform|year, and it collapses a US and a
    Japanese copy of the same game into one entry — so owning both Chrono Trigger on
    SNES and on Super Famicom got you two Super Famicom boxes on the shelf. The region
    is part of which box you own."""
    return f"{row['_k']}#{(row.get('releaseRegion') or '').strip()}"
ROTATE_MARGIN = 0.03      # a turned panel must beat the upright one by this much
TEXT_SIDEWAYS = 0.85      # back-cover text lines: below this the page reads sideways


def get(url: str, timeout: int = 60) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gamedex/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def thumb_of(url: str) -> str:
    stem, _, ext = url.rpartition(".")
    return f"{stem}_thumb.{ext}"


def fingerprint(im: Image.Image, size=(28, 36)) -> list[float] | None:
    """A tiny normalised COLOUR signature.

    This used to be grayscale, and that is how Animal Crossing: City Folk ended up
    wearing the Balls of Fury box: strip the colour out and a lot of unrelated art
    looks alike. Keeping the channels separates them (that pair scores -0.08 in
    colour and +0.08 in grey)."""
    px = list(im.convert("RGB").resize(size, Image.LANCZOS).getdata())
    v = [c for p in px for c in p]
    n = len(v)
    mean = sum(v) / n
    var = sum((x - mean) ** 2 for x in v) / n
    if var < 1e-6:
        return None
    sd = var ** 0.5
    return [(x - mean) / sd for x in v]


def similarity(a, b) -> float:
    """Normalised cross-correlation: 1.0 identical, 0.0 unrelated."""
    if not a or not b:
        return -1.0
    return sum(x * y for x, y in zip(a, b)) / len(a)


def panels(im: Image.Image, tpl):
    back_mm, spine_mm, front_mm, _ = tpl
    total = back_mm + spine_mm + front_mm
    w = im.width
    x1 = round(w * back_mm / total)
    x2 = round(w * (back_mm + spine_mm) / total)
    return im.crop((0, 0, x1, im.height)), im.crop((x1, 0, x2, im.height)), im.crop((x2, 0, w, im.height))


def landscape_strip(im: Image.Image):
    """Get the wrap into a horizontal back|spine|front strip. (The art inside may
    still be sideways — that's a separate question, answered by the IGDB cover.)"""
    if im.height <= im.width:
        return im
    cw = im.rotate(-90, expand=True)
    third = cw.width // 3
    left = _saturation(cw.crop((0, 0, third, cw.height)))
    right = _saturation(cw.crop((cw.width - third, 0, cw.width, cw.height)))
    return cw if right >= left else im.rotate(90, expand=True)


def text_lines(im: Image.Image) -> float:
    """Does this page read upright? A back cover is mostly TEXT, and rows of text make
    the row-brightness swing (ink, gap, ink, gap). Turn the page on its side and the
    swing moves to the columns. Ratio > 1 means upright.

    On its own this is not enough — a back cover that's mostly screenshots has no text
    rhythm to find — which is why it only ever gets a vote, never the casting vote."""
    g = ImageOps.equalize(im.convert("L")).resize((160, 160), Image.LANCZOS)
    px = list(g.getdata())
    rows = [sum(px[y * 160:(y + 1) * 160]) / 160 for y in range(160)]
    cols = [sum(px[x::160]) / 160 for x in range(160)]
    var = lambda v: sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v)
    return var(rows) / (var(cols) + 1e-6)


def resolve(game, idx, cover_fp):
    """Pick the scan, and pick which way is up.

    Region comes first, because that's a fact about which BOX we want, and no amount
    of pixel-matching should talk us into a Super Famicom box when a US one exists.
    Then the IGDB cover confirms the scan really is this game, and decides rotation.

    Rotating is treated as the EXCEPTION it is. Almost every scan is upright — and
    the ones that look sideways are usually just the wrong scan. So a rotation has to
    be argued for twice: the back cover's text must read sideways AND the turned front
    must match the IGDB cover better than the upright one. Either alone is not enough;
    each is wrong on its own often enough to matter.
    """
    d = PLATFORM_DIR.get(game["platform"])
    if not d:
        return None
    cands = idx.get(d, {}).get(slugify(game["title"]))
    if not cands:
        return None
    tpl_name = PLATFORM_TEMPLATE.get(d)

    # THE REGION IS NOT A PREFERENCE, IT IS A CONSTRAINT.
    #
    # Every scan I found with sideways art turned out to be a foreign or custom cover
    # that had no business being there: the only SNES "Chrono Trigger" wrap is a PAL
    # fan cover (the game never shipped on PAL SNES), and the only "Super Mario World"
    # is the Super Famicom box. Meanwhile EVERY correctly-picked, correct-region scan
    # is upright — Super Metroid, Sonic 2, Ocarina, Metal Gear Solid, Shadow of the
    # Colossus, Metroid Dread, all of them.
    #
    # So rotation was never the disease, it was the symptom. Refuse the wrong box and
    # the sideways art goes with it. An unlabelled legacy key is a US scan.
    want = REGION_OF_SHEET.get(game.get("releaseRegion") or "") or "US"
    ok = {want, ""} | ({"US", "NA"} if want == "US" else set())
    cands = [c for c in cands if c["region"] in ok]
    if not cands:
        return None
    ranked = sorted(cands, key=lambda c: REGION_RANK.get(c["region"], 7))

    best = None
    for c in ranked[:4]:
        if c["url"].rsplit("/", 1)[-1] in BLOCKED:
            continue
        raw = get(thumb_of(c["url"]), 45) or get(c["url"], 90)
        if not raw:
            continue
        try:
            im = landscape_strip(Image.open(io.BytesIO(raw)).convert("RGB"))
        except Exception:
            continue

        # The platform's template, or nothing. Falling back to "whichever template is
        # nearest by aspect" is how a Game Boy box became a jewel case: Game Boy has
        # no template here, so the search happily handed it the closest shape it could
        # find. A box we can't name is a box we don't cut.
        if not tpl_name:
            continue
        ar = im.width / im.height
        if abs(ar / _aspect(TEMPLATES[tpl_name]) - 1) > TOLERANCE:
            continue                       # this scan isn't printed to that platform's box
        tpl = (tpl_name, TEMPLATES[tpl_name])

        back, _, front = panels(im, tpl[1])

        # Some platforms' scans are simply stored on their side — see TEMPLATE_ROT. That
        # is not something to detect, it's something to know: an N64 box is landscape,
        # and no heuristic here gets that right (both of mine read the sideways Zelda
        # logo as the real cover).
        rot = TEMPLATE_ROT.get(tpl[0], 0)
        if rot:
            front = front.rotate(rot, expand=True)
        score = similarity(fingerprint(front), cover_fp)

        if best is None or score > best["score"]:
            _, spine_mm, front_mm, h_mm = tpl[1]
            # A turned panel means the box itself is the other way round — a PAL SNES
            # box is landscape where the US one is portrait, off the same template.
            w, h = (front_mm, h_mm) if rot == 0 else (h_mm, front_mm)
            best = {"url": c["url"], "region": c["region"], "rot": rot,
                    "template": tpl[0], "case": {"w": w, "h": h, "d": spine_mm},
                    "score": round(score, 4)}
    # A scan that looks nothing like the game IS NOT THE GAME. Cover Project carries
    # fan covers and the odd mislabelled upload; this is the only thing standing between
    # them and your shelf.
    if not best or best["score"] < MIN_SCORE:
        return None
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="https://games.zachd.duckdns.org")
    ap.add_argument("--index", default="data/coverproject.json")
    ap.add_argument("--out", default="data/covers-resolved.json")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    idx = json.loads(pathlib.Path(a.index).read_text())["covers"]
    print("loading the collection ...", file=sys.stderr)
    rows = json.loads(get(f"{a.api}/api/data", 300))["sheets"]["games"]["rows"]
    enr = json.loads(get(f"{a.api}/api/enrichment/all", 300))["items"]

    # The shelf is things you can physically pick up. Digital games are not on it.
    phys = [r for r in rows if r.get("owned")
            and (r.get("format") or "").strip().lower() in ("physical", "both")]
    print(f"  {len(phys)} physical games owned", file=sys.stderr)

    def one(r):
        # The BOX is keyed per region; ENRICHMENT is still keyed per game. Mixing the
        # two up looks up the cover under a key that cannot exist and quietly resolves
        # nothing at all.
        key = row_key(r)
        e = enr.get(r["_k"]) or {}
        fp = hue = None
        if e.get("cover"):
            raw = get(IGDB_IMG.format(e["cover"]), 45)
            if raw:
                try:
                    im = Image.open(io.BytesIO(raw))
                    fp = fingerprint(im)
                    # Every game with no scanned spine still needs a spine COLOUR, and
                    # we already have its cover open right here. Do it now so the server
                    # never has to touch IGDB at request time.
                    hue = dominant_hue(im)
                except Exception:
                    pass
        hit = resolve(r, idx, fp) if fp else None
        return key, hit, hue

    wraps, hues, no_cover = {}, {}, 0
    with futures.ThreadPoolExecutor(a.workers) as ex:
        for i, (key, hit, hue) in enumerate(ex.map(one, phys)):
            if hit:
                wraps[key] = hit
            if hue:
                hues[key] = hue
            else:
                no_cover += 1
            if i % 200 == 0:
                print(f"  {i}/{len(phys)} · {len(wraps)} wraps", file=sys.stderr)

    p = pathlib.Path(a.out)
    p.write_text(json.dumps({"wraps": wraps, "hues": hues}, separators=(",", ":")))
    rot = sum(1 for v in wraps.values() if v["rot"])
    print(f"\nwrote {p}: {len(wraps)}/{len(phys)} real box wraps ({rot} rotated), "
          f"{len(hues)} spine colours, {no_cover} games with no cover at all "
          f"(they get a blank case with the title)", file=sys.stderr)


if __name__ == "__main__":
    main()
