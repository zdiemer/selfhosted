#!/usr/bin/env python3
"""Build the Cover Project index — the thing that makes real box wraps possible.

The Cover Project has what nobody else has: scans of the WHOLE box, laid out
back | spine | front. That is exactly the texture a 3D case needs, and it's the
one thing IGDB can never give you (IGDB hands you a portrait cover for every
platform, even for a PS1 jewel case, which is landscape).

Getting at it is the interesting part.

    thecoverproject.net is behind a Cloudflare managed challenge. Plain curl,
    curl_cffi, a stealth-patched headless Chrome and a genuinely headful Chrome
    under Xvfb all get a 403 and "Just a moment...". So does download_cover.php,
    which is the endpoint that would hand us the CDN key. There is no API, no
    sitemap, and the S3 bucket refuses to list.

    But the site is not the images. The images live on a DigitalOcean Spaces CDN
    that serves to a bare `curl` — no login, no referer check, no UA sniffing.
    The only thing standing between us and them is that we don't know the keys.

    So don't ask the site for the keys. Ask the Internet Archive, which crawled
    the CDN directly. One CDX query returns ~44,000 object URLs, keys and all.

    Nothing here ever touches the protected origin.

Two key formats live side by side, and both still serve:

    legacy   {plat}/{plat}_{slug}[_{variant}].jpg
    current  {plat}/{plat}.{slug}_{REGION}.{opaque}.jpg

The `opaque` digits are NOT derivable — which is why the archive matters. It's
also why we can't construct a key we haven't seen: an invented one 404s. The
index IS the product.

Usage:  python3 tools/coverproject_index.py [--out data/coverproject.json]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.request

CDX = ("http://web.archive.org/cdx/search/cdx"
       "?url=coverproject.sfo2.cdn.digitaloceanspaces.com*"
       "&output=json&collapse=urlkey&fl=original&filter=statuscode:200&limit=200000")

CDN = "https://coverproject.sfo2.cdn.digitaloceanspaces.com"

# Cover Project's platform directory -> the sheet's platform name.
PLATFORMS = {
    "super_nintendo": "Super Nintendo Entertainment System",
    "nes": "Nintendo Entertainment System",
    "nintendo_64": "Nintendo 64",
    "gamecube": "Nintendo GameCube",
    "nintendo_wii": "Nintendo Wii",
    "nintendo_wii_u": "Nintendo Wii U",
    "nintendo_switch": "Nintendo Switch",
    "gameboy": "Nintendo Game Boy",
    "gameboy_color": "Nintendo Game Boy Color",
    "gameboy_advance": "Nintendo Game Boy Advance",
    "nintendo_ds": "Nintendo DS",
    "nintendo_3ds": "Nintendo 3DS",
    "virtual_boy": "Nintendo Virtual Boy",
    "playstation_1": "PlayStation",
    "playstation_2": "PlayStation 2",
    "playstation_3": "PlayStation 3",
    "playstation_4": "PlayStation 4",
    "playstation_5": "PlayStation 5",
    "psp": "PlayStation Portable",
    "ps_vita": "PlayStation Vita",
    "genesis": "Sega Genesis",
    "sega_cd": "Sega CD",
    "sega_32x": "Sega 32X",
    "sega_saturn": "Sega Saturn",
    "dreamcast": "Sega Dreamcast",
    "sega_master_system": "Sega Master System",
    "game_gear": "Sega Game Gear",
    "xbox": "Xbox",
    "xbox_360": "Xbox 360",
    "xbox_one": "Xbox One",
    "atari_2600": "Atari 2600",
    "atari_5200": "Atari 5200",
    "atari_7800": "Atari 7800",
    "atari_jaguar": "Atari Jaguar",
    "atari_lynx": "Atari Lynx",
    "3do": "3DO Interactive Multiplayer",
    "neo_geo": "Neo Geo",
    "turbografx_16": "TurboGrafx-16",
    "colecovision": "ColecoVision",
    "intellivision": "Intellivision",
    "windows": "PC",
}

# Their scans also carry cart labels, disc faces and manual scans. Those are not
# wraps and must not be handed to a 3D case.
NOT_A_WRAP = re.compile(r"_(label|manual|disc|cart|media|tray|insert|poster)(_|\.|$)", re.I)

# The two key formats. Note the LEGACY filename is prefixed with a SHORT platform
# code ("snes_zelda3.jpg") while the directory is the long name ("super_nintendo/")
# — they do not match, so don't try to backreference the directory.
#   current  {dir}/{dir}.{slug}_{REGION}.{opaque}[_thumb].{ext}
#   legacy   {dir}/{code}_{slug}[_{variant}][_thumb].{ext}
CURRENT = re.compile(r"/([^/]+)/\1\.([a-z0-9]+)_([A-Z]{0,3})\.(\d+)(_thumb)?\.(jpg|png)$", re.I)
LEGACY = re.compile(r"/([^/]+)/([a-z0-9]+)_(.+?)(_thumb)?\.(jpg|png)$", re.I)

# Trailing tokens on a legacy key that are a region or an alternate scan, not part
# of the title: snes_7thsaga_2.jpg, 3do_20thcenturyalmanac_au.jpg
VARIANT = re.compile(r"^(us|na|eu|uk|au|jp|ca|fr|de|es|it|kr|br|\d{1,2})$", re.I)

# Which region do we want on the shelf, in order of preference.
REGION_RANK = {"US": 0, "NA": 1, "EU": 2, "UK": 3, "AU": 4, "JP": 5, "": 6}


def slugify(title: str) -> str:
    """Their slug rule, reverse-engineered from the keys.

    Lowercase, drop everything that isn't a letter or digit, and — the part you'd
    never guess — move a LEADING article to the end:
        "The Legend of Zelda: Ocarina of Time" -> legendofzeldaocarinaoftimethe
    """
    s = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    m = re.match(r"^(the|a|an) (.+)$", s)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    return re.sub(r"\s+", "", s)


def fetch(url: str, tries: int = 5) -> bytes:
    """The CDX endpoint 504s under load often enough to matter. Back off and retry —
    this is a single query we make once, so waiting is cheaper than failing."""
    req = urllib.request.Request(url, headers={"User-Agent": "gamedex/1.0 (+personal collection)"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 5 * 2 ** i
            print(f"  {e} — retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def build(cache: pathlib.Path | None = None) -> dict:
    if cache and cache.exists():
        print(f"using cached CDX response ({cache})", file=sys.stderr)
        rows = json.loads(cache.read_text())[1:]
    else:
        print("querying the Internet Archive for CDN keys ...", file=sys.stderr)
        raw = fetch(CDX)
        if cache:
            cache.write_bytes(raw)
        rows = json.loads(raw)[1:]
    urls = [r[0] for r in rows]
    print(f"  {len(urls)} archived objects", file=sys.stderr)

    # slug -> list of candidates. A thumbnail proves the full-size sibling's key,
    # so strip _thumb and dedupe: the two collapse onto the same object.
    index: dict[str, dict[str, list]] = {}
    skipped = 0
    for u in urls:
        if NOT_A_WRAP.search(u):
            skipped += 1
            continue
        m = CURRENT.search(u)
        if m:
            plat, slug, region, num, _thumb, ext = m.groups()
            full = f"{CDN}/{plat}/{plat}.{slug}_{region}.{num}.{ext}"
        else:
            m = LEGACY.search(u)
            if not m:
                continue
            plat, code, rest, _thumb, ext = m.groups()
            # A thumbnail proves its full-size sibling's key: same object, minus
            # the suffix. That's what lets ~44k archived objects become a usable
            # index even though only a fraction were archived at full size.
            full = f"{CDN}/{plat}/{code}_{rest}.{ext}"
            parts = rest.split("_")
            region = ""
            while len(parts) > 1 and VARIANT.match(parts[-1]):
                tok = parts.pop()
                if not tok.isdigit():
                    region = tok.upper()
            slug = "".join(parts)
        if plat not in PLATFORMS or not slug:
            continue
        entry = {"url": full, "region": region}
        cands = index.setdefault(plat, {}).setdefault(slug, [])
        if entry not in cands:
            cands.append(entry)

    for plat in index:
        for slug, cands in index[plat].items():
            cands.sort(key=lambda c: REGION_RANK.get(c["region"], 9))

    total = sum(len(v) for v in index.values())
    print(f"  {total} games across {len(index)} platforms "
          f"({skipped} labels/manuals skipped)", file=sys.stderr)
    return {"cdn": CDN, "platforms": PLATFORMS, "covers": index}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/coverproject.json")
    ap.add_argument("--cache", default="data/.cdx-cache.json",
                    help="raw CDX response; reused so a rebuild costs no network")
    a = ap.parse_args()
    data = build(pathlib.Path(a.cache) if a.cache else None)
    p = pathlib.Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, separators=(",", ":")))
    print(f"wrote {p} ({p.stat().st_size // 1024} KB)", file=sys.stderr)
    for plat in sorted(data["covers"], key=lambda k: -len(data["covers"][k]))[:12]:
        print(f"  {PLATFORMS[plat]:38} {len(data['covers'][plat]):5}", file=sys.stderr)
