#!/usr/bin/env python3
"""Which games are on the NAS — answered from the download receipts, not from a scan.

The ROM library is ~80TiB on a CIFS share. Walking it to answer "do I have this one?" is absurd,
and we don't have to: romnas already writes a `.romnas-download.json` at each system's root,
recording every file it fetched. 162 of them, 8.6M files, and they read in seconds.

So this reads the receipts, joins them against gamedex's game list on (platform, title), and POSTs
the result. It runs on the WORKSTATION — the k3s nodes can't see the CIFS mount and shouldn't have
to — and upgrade.sh invokes it after a rollout, so the answer is refreshed on every deploy.

THE THING THAT MATTERS IS NOT CLAIMING WHAT WE DON'T KNOW. Three states, not two:

  on the NAS      the title is in that system's receipt
  not on the NAS  the system IS name-indexed, and the title isn't in it
  not indexed     we cannot honestly answer — see OPAQUE

Wii U records its games as title-id folders (`0005000010169e00`), and the Xbox 360 receipt holds
only `_digital` DLC. Title-matching either one produces confident nonsense: a 360 game would come
back "on the NAS" because its DLC's name matched. Those systems say "not indexed" and mean it.

    python3 tools/nas_index.py                          # -> POST to $GAMEDEX_URL
    python3 tools/nas_index.py --dry-run --stats        # match rates, writes nothing
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.request
from collections import Counter

ROOT = pathlib.Path(os.environ.get("NAS_ROMS_ROOT", "/mnt/games/Roms"))
LOCKFILE = ".romnas-download.json"

# The sheet's name for a machine, and romnas's folder for the same machine. Anything not listed
# matches by name (normalised), which covers ~85 of them — these are the ones that disagree.
FOLDER = {
    "PlayStation": "PSX", "PlayStation 2": "PS2", "PlayStation 3": "PS3",
    "PlayStation Portable": "PSP", "PlayStation Vita": "PS Vita",
    "Sega Genesis": "Genesis", "Sega Saturn": "Saturn", "Sega Master System": "Master System",
    "Sega Dreamcast": "Dreamcast", "Sega Game Gear": "Game Gear",
    "Nintendo GameCube": "GameCube", "Nintendo Wii": "Wii", "Nintendo Wii U": "Wii U",
    "Nintendo Entertainment System": "NES", "Super Nintendo": "SNES",
    "Super Nintendo Entertainment System": "SNES",
    "Nintendo Virtual Boy": "Virtual Boy",
    "Commodore Amiga": "Amiga", "Commodore Amiga CD32": "Amiga CD32",
    "Commodore VIC-20": "VIC-20", "Commodore Plus/4": "Commodore Plus-4",
    "Philips CD-i": "CD-i", "Atari Jaguar": "Jaguar", "Atari Lynx": "Lynx",
    "Microsoft Xbox": "Xbox", "Microsoft Xbox 360": "Xbox 360",
    "NEC TurboGrafx-16": "TurboGrafx-16", "NEC TurboGrafx-CD": "TurboGrafx-CD",
    "NEC PC-9801": "PC-98", "NEC PC-8801": "PC-88", "NEC PC-FX": "PC-FX",
    "Neo-Geo Pocket": "Neo Geo Pocket", "Neo-Geo Pocket Color": "Neo Geo Pocket Color",
    "SNK Neo Geo CD": "Neo Geo CD",
    "Bandai WonderSwan": "WonderSwan", "Bandai WonderSwan Color": "WonderSwan Color",
    "Arcade": "MAME",
    # The sheet's "PC" is mostly modern Steam-era; the NAS's PC library is the DOS era. The join is
    # still right — a modern PC game genuinely isn't there — it just means most PC rows say no.
    "PC": "MS-DOS",
}

# Systems whose receipt does not name its games. See the module docstring: matching these produces
# confident nonsense, so they get "not indexed" instead of a wrong answer.
OPAQUE = {
    "Wii U",      # title-id folders: 0005000010169e00
    "MAME",       # the receipt records the artwork/extras download, not the romsets
    "Xbox 360",   # only `_digital` DLC and add-ons — never the games themselves
    "J2ME",       # 23k titles in the receipt, zero of 83 match: its naming isn't the sheet's
}

# A DISC KEY IS NOT A GAME. The PS3 receipt carries 4,715 `_disckeys` — 238-byte files that decrypt
# a disc you have to own separately — against only 1,717 actual ISOs. Treat a key as a game and PS3
# reports two thousand titles it does not have, each one confidently, with a size of 238 B. The
# bucket is metadata about games; it is not the games.
BUCKET_SKIP = {"_disckeys", "_psn"}     # _psn pkgs are named by product code, never by title

# Not the game: DLC, add-ons, updates, patches. A DLC's name is its parent game's name, so leaving
# these in makes "on the NAS" fire on games we only hold add-ons for.
NOT_A_GAME = re.compile(r"\((?:Addon|DLC|Update|Patch|Demo|Beta|Proto|Sample)\b", re.I)

EXT = re.compile(
    r"\.(zip|7z|rar|chd|iso|bin|cue|rvz|nsp|xci|z64|n64|v64|sfc|smc|nes|gb[ac]?|gg|md|sms|pce|"
    r"cso|wud|wux|rpx|3ds|cia|cxi|nds|gcm|gcz|wbfs|cdi|gdi|dsk|adf|d64|tap|conf|exe|ipa|apk|"
    r"hdi|hdm|m3u|pkg|st|adz|dms|lha|tzx|tap|prg|crt|a26|a78|lnx|j64|vb|ws[c]?|ngp|ngc)$", re.I)

# The sheet writes Japanese long vowels with macrons (Kōryū no Mimi); the ROM sets spell them out
# (kouryuu no mimi). Expand BEFORE stripping diacritics — strip first and the macron collapses to a
# bare vowel, so the two spellings never meet. This one rule moved the consoles from ~75-89% to
# 86-94%, and nearly every remaining miss is a game that genuinely isn't there.
MACRON = {"ō": "ou", "ū": "uu", "ā": "aa", "ē": "ee", "ī": "ii",
          "Ō": "ou", "Ū": "uu", "Ā": "aa", "Ē": "ee", "Ī": "ii"}


def norm(t: str, squash: bool = False) -> str:
    for k, v in MACRON.items():
        t = (t or "").replace(k, v)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"^(.*?),\s*(the|a|an)\b", r"\2 \1", t)      # "Legend of Zelda, The" -> "the legend…"
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t.replace(" ", "") if squash else t              # squashed key catches "ninjawarriors"


def rom_title(path: str) -> str | None:
    """The game a recorded file belongs to.

    romnas buckets some systems under `_nus` / `_hacks` / `_decrypted`, and shelves others one
    directory per game (MS-DOS). So: drop the bucket, and the game is the first segment left —
    which is the folder when there is one and the file itself when there isn't.
    """
    parts = [s for s in path.split("/") if s]
    if parts and parts[0] in BUCKET_SKIP:
        return None
    segs = [s for s in parts if not s.startswith("_")]
    if not segs:
        return None
    name = segs[0]
    if NOT_A_GAME.search(name):
        return None
    # iOS dumps are "Title-(com.bundle.id)-1.0-(iOS_3.1)-<hash>.ipa"
    name = re.sub(r"-\(\w+(\.\w+)+\)-.*$", "", name)
    name = EXT.sub("", name)
    name = re.sub(r"\s*\([^()]*\)", "", name)               # (USA), (En,Fr,De), (Rev 1), (2002)
    name = re.sub(r"\s*\[[^\]]*\]", "", name)               # [T-En by …], [titleid]
    return name.strip() or None


CACHE = pathlib.Path(os.environ.get(
    "NAS_INDEX_CACHE", "~/.cache/gamedex/nas-index-cache.json")).expanduser()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, CACHE)


def read_system(folder: str, cache: dict) -> tuple[set, set, dict]:
    """One system's receipt -> (titles, squashed titles, title -> {file, size}).

    Cached on the receipt's own mtime+size. A cold run is ~50s — MS-DOS alone records 807k files —
    and this sits in the deploy path, where 50s every time is a tax for nothing: the receipts only
    change when romnas actually runs. Warm, it's a second.
    """
    lf = ROOT / folder / LOCKFILE
    if not lf.is_file():
        return set(), set(), {}
    try:
        st = lf.stat()
        stamp = f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        return set(), set(), {}
    hit = cache.get(folder)
    if hit and hit.get("stamp") == stamp:
        return set(hit["exact"]), set(hit["squashed"]), hit["meta"]
    try:
        data = json.loads(lf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set(), set(), {}
    exact, squashed, meta = set(), set(), {}
    for src in (data.get("sources") or {}).values():
        for f in (src.get("files") or []):
            name = f.get("name") if isinstance(f, dict) else f
            if not name:
                continue
            title = rom_title(name)
            if not title:
                continue
            k = norm(title)
            if not k:
                continue
            exact.add(k)
            squashed.add(norm(title, True))
            # The game's own segment — NOT name.split("/")[0], which is romnas's bucket (`_nus`,
            # `_disckeys`). And keep the BIGGEST file under that title: on a folder-per-game system
            # the first file you meet is a .conf or a disc key, and reporting "285 B" as the game's
            # size is worse than reporting nothing.
            segs = [x for x in name.split("/") if x and not x.startswith("_")]
            shown = segs[0] if segs else name
            size = (f.get("size") or 0) if isinstance(f, dict) else 0
            cur = meta.get(k)
            if cur is None or size > cur["size"]:
                meta[k] = {"file": shown, "size": size}
    cache[folder] = {"stamp": stamp, "exact": sorted(exact),
                     "squashed": sorted(squashed), "meta": meta}
    return exact, squashed, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GAMEDEX_URL", "https://games.zachd.duckdns.org"))
    ap.add_argument("--token", default=os.environ.get("NAS_TOKEN", ""))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if not ROOT.is_dir():
        print(f"nas-index: {ROOT} not mounted — skipping (this is fine off the workstation)")
        return 0

    t0 = time.time()
    with urllib.request.urlopen(f"{args.url}/api/data", timeout=120) as r:
        rows = json.loads(r.read())["sheets"]["games"]["rows"]

    have = {p.name for p in ROOT.iterdir() if p.is_dir()}
    by_norm = {norm(f): f for f in have}

    def folder_for(platform: str) -> str | None:
        f = FOLDER.get(platform)
        if f and f in have:
            return f
        return by_norm.get(norm(platform))

    # Only read the receipts we can actually use. The full set is 8.6M file entries and 51s, and
    # most of that is systems nothing maps to (TeknoParrot alone is 3.4M) or that we won't claim on.
    wanted, opaque_plats = {}, set()
    for r in rows:
        p = r.get("platform")
        if not p or p in wanted:
            continue
        f = folder_for(p)
        if f in OPAQUE:
            opaque_plats.add(p)
        wanted[p] = None if (not f or f in OPAQUE) else f

    disk = _load_cache()
    before = len(disk), sum(len(v.get("exact", [])) for v in disk.values())
    cache: dict[str, tuple[set, set, dict]] = {}
    for f in sorted({v for v in wanted.values() if v}):
        cache[f] = read_system(f, disk)
    if (len(disk), sum(len(v.get("exact", [])) for v in disk.values())) != before:
        _save_cache(disk)

    index, unindexed = {}, sorted(opaque_plats)
    stats = Counter()
    per = {}
    for r in rows:
        p, t, k = r.get("platform"), r.get("title"), r.get("_k")
        if not (p and t and k):
            continue
        f = wanted.get(p)
        if f is None:
            stats["unindexed" if p in unindexed else "absent"] += 1
            continue
        exact, squashed, meta = cache[f]
        key = norm(t)
        hit = key in exact
        if not hit and norm(t, True) in squashed:
            hit, key = True, None
        if hit:
            m = meta.get(key or norm(t), {})
            index[k] = {"system": f, "file": m.get("file", ""), "size": m.get("size", 0)}
            stats["on_nas"] += 1
        else:
            stats["absent"] += 1
        d = per.setdefault(p, [0, 0])
        d[0 if hit else 1] += 1

    payload = {
        "generatedAt": int(time.time()),
        "systems": sorted(cache),
        "unindexed": unindexed,          # platforms we refuse to answer for
        "games": index,
    }
    took = time.time() - t0
    print(f"nas-index: {stats['on_nas']:,} on the NAS · {stats['absent']:,} not · "
          f"{stats['unindexed']:,} unindexed ({len(unindexed)} platforms) · "
          f"{len(cache)} receipts read in {took:.1f}s")

    if args.stats:
        print("\nper-platform (>40 games):")
        for p, (h, m) in sorted(per.items(), key=lambda x: -(x[1][0] + x[1][1])):
            if h + m > 40:
                print(f"  {p:34} {h:4}/{h+m:4}  {h/(h+m):3.0%}")

    if args.dry_run:
        return 0
    if not args.token:
        print("nas-index: no NAS_TOKEN — refusing to post (set nas.token in values.local.yaml)",
              file=sys.stderr)
        return 1

    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{args.url}/api/nas", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Nas-Token": args.token})
    with urllib.request.urlopen(req, timeout=120) as r:
        print("nas-index:", json.loads(r.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
