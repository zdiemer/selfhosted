"""Bake the GameRankings archive into a lookup gamedex can serve.

GameRankings shut down in 2019, so this data is FROZEN — there is nothing to poll and
nothing to scrape at runtime. A community export of the whole site lives in a public
Google Sheet; we pull it once, normalise it, and emit data/gamerankings.json, which the
image carries. Re-run only if the sheet is ever updated.

    python3 tools/gamerankings.py            # -> data/gamerankings.json

The sheet's own site is dead; the archive at gr.blade.sk mirrors the same URL paths, so we
keep the path and build the archive link from it.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import sys
import urllib.request

SHEET_ID = "1wtV8yBr5RXAjO_1kakzFcHd7TKGw7pIUVNI5SYouCUM"
GID = "782449831"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "gamerankings.json"

# GameRankings' platform code -> every gamedex platform name it can serve. A code maps to
# several names because the sheet spells the same machine more than one way.
PLATFORMS = {
    "PC": ["PC"],
    "PS": ["PlayStation"],
    "PS2": ["PlayStation 2"],
    "PS3": ["PlayStation 3"],
    "PS4": ["PlayStation 4"],
    "PSP": ["PlayStation Portable", "PSP"],
    "VITA": ["PlayStation Vita", "PS Vita"],
    "XBOX": ["Xbox"],
    "X360": ["Xbox 360"],
    "XONE": ["Xbox One"],
    "NS": ["Nintendo Switch"],
    "WII": ["Nintendo Wii", "Wii"],
    "WIIU": ["Nintendo Wii U", "Wii U"],
    "GC": ["Nintendo GameCube", "GameCube"],
    "N64": ["Nintendo 64"],
    "SNES": ["SNES", "Super Nintendo Entertainment System"],
    "GB": ["Game Boy", "Nintendo Game Boy"],
    "GBC": ["Game Boy Color", "Nintendo Game Boy Color"],
    "GBA": ["Game Boy Advance", "Nintendo Game Boy Advance"],
    "DS": ["Nintendo DS"],
    "3DS": ["Nintendo 3DS", "New Nintendo 3DS"],
    "GEN": ["Sega Genesis", "Genesis"],
    "SAT": ["Sega Saturn", "Saturn"],
    "DC": ["Sega Dreamcast", "Dreamcast"],
    "SCD": ["Sega CD"],
    # IOS / MOBI / MAC / NGE are left out: nothing in the collection lines up with them.
}


def norm(t: str) -> str:
    """Title -> comparison key. Must stay in step with src/gamerankings.py."""
    t = (t or "").lower().replace("’", "").replace("'", "")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return re.sub(r"\s+", " ", t)


def main() -> int:
    print(f"fetching {CSV_URL}")
    raw = urllib.request.urlopen(CSV_URL, timeout=120).read().decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    print(f"  {len(rows)} rows")

    out: dict[str, list] = {}
    skipped = 0
    for r in rows:
        code = (r.get("platform") or "").strip()
        names = PLATFORMS.get(code)
        if not names:
            skipped += 1
            continue
        try:
            score = float(r.get("avg score") or 0)
        except ValueError:
            continue
        if not score:
            continue
        try:
            n = int(float(r.get("reviews") or 0))
        except ValueError:
            n = 0
        # keep the site path, so the frontend can link the gr.blade.sk archive
        path = ""
        m = re.search(r"gamerankings\.com/(.+?)(?:/index\.html)?$", r.get("link") or "")
        if m:
            path = m.group(1)
        key_title = norm(r.get("title"))
        if not key_title:
            continue
        for name in names:
            k = f"{key_title}|{name}"
            # Highest review count wins a duplicate — the better-sampled entry.
            if k not in out or n > out[k][1]:
                out[k] = [round(score, 2), n, path]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {OUT}  ({len(out)} keys, {OUT.stat().st_size/1e6:.1f} MB)")
    print(f"  skipped {skipped} rows on platforms we don't carry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
