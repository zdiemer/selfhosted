"""GameTDB — the printed face of the disc itself.

IGDB gives you box art. It has never given you what's actually IN the box, and for a disc that's
a shame: the printed disc face is its own artwork, and on a shelf where the boxes open it's the
thing you'd actually be looking at.

GameTDB publishes real disc scans, free and unauthenticated, keyed on the platform's own game id
(`RMGE01`). We don't have that id — RomM doesn't record serials (checked) — so we take their full
database dump, which maps id to title, and join on the title.

Wii and GameCube live in the same dump (`wiitdb.xml`, ~5,000 games), which is convenient, because
those are the two platforms whose discs are worth looking at. PS3 and Switch have their own dumps
and box art but no disc art worth the trouble.

Cached on the PVC: the dump is ~1.9MB zipped and changes rarely, so it's fetched once and refreshed
on a version bump.
"""

from __future__ import annotations

import io
import json
import logging
import pathlib
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

log = logging.getLogger("gamedex.gametdb")

_UA = "gamedex/1.0 (personal game collection; +https://github.com/zdiemer)"

# GAMECUBE IS BEHIND A QUERY FLAG, and finding that out cost an hour, so: plain `wiitdb.zip` is
# Wii only — its 88 GameCube-looking ids are homebrew hacks ("Smash Melee: SD Remix"), which is
# exactly the sort of thing that gets you the wrong disc face and no error. `gcntdb.zip` looks
# like a GameCube dump and returns HTTP 200, but the body is an HTML page, not a zip.
# `wiitdb.zip?GAMECUBE=1` is the real thing: 6,714 games, 1,650 of them GameCube.
#
# The dump doesn't label which console a game is, so classify on the id prefix — GameCube ids
# begin G or D, Wii ids begin R, S, C… — and filter a lookup to the platform that asked, so a Wii
# re-release can never answer for a GameCube game (Metroid Prime, otherwise, resolves to the Wii
# "New Play Control" pressing and you get its disc).
DUMPS = {
    # url, art folder on art.gametdb.com
    "wii+gamecube": ("https://www.gametdb.com/wiitdb.zip?LANG=EN&GAMECUBE=1", "wii"),
    "wiiu": ("https://www.gametdb.com/wiiutdb.zip?LANG=EN", "wiiu"),
}
_GC_PREFIX = ("G", "D")           # GameCube discs (and GameCube demo discs)
ART = "https://art.gametdb.com/{path}/{kind}/{region}/{gid}.png"

# The sheet's names for the platforms whose discs GameTDB actually scans.
PLATFORMS = {"Nintendo Wii": "wii", "Nintendo GameCube": "gamecube", "Nintendo Wii U": "wiiu"}

# GameTDB's art is filed by a language/region folder, not by the XML's region code.
_REGION_DIR = {"NTSC-U": "US", "NTSC-J": "JA", "PAL": "EN", "NTSC-K": "KO", "NTSC-T": "ZH"}
_REGION_ORDER = ["US", "EN", "JA"]        # try mine first, then Europe, then Japan


def _norm(s: str) -> str:
    """Match keys. Aggressive on purpose — GameTDB titles carry subtitle punctuation and
    trademark noise that our sheet doesn't."""
    s = (s or "").lower()
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


class GameTdb:
    def __init__(self, cache_dir: str = "/data/gametdb", ttl_days: int = 30):
        self._dir = pathlib.Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "wiitdb.json"
        self._ttl = ttl_days * 86400
        self._map: dict[str, list[dict]] = {}
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._load()

    @property
    def ready(self) -> bool:
        return bool(self._map)

    # -- the dump ------------------------------------------------------------
    def _load(self) -> None:
        try:
            blob = json.loads(self._path.read_text())
            if time.time() - blob.get("fetched", 0) < self._ttl and blob.get("version") == 2:
                self._map = blob["games"]
                log.info("gametdb: %d titles from cache", len(self._map))
                return
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("gametdb: cache unreadable (%s)", exc)
        self.refresh()

    @staticmethod
    def _console_of(gid: str, dump: str) -> str:
        """Which machine a disc actually belongs to. The dump won't say; the id prefix will."""
        if dump == "wiiu":
            return "wiiu"
        return "gamecube" if gid[:1] in _GC_PREFIX else "wii"

    def _parse(self, xml: bytes, dump: str, art_path: str, games: dict) -> int:
        try:
            root = ET.fromstring(xml)
        except Exception as exc:
            log.warning("gametdb: %s dump unparseable (%s)", dump, exc)
            return 0
        n = 0
        for g in root.iter("game"):
            gid = (g.findtext("id") or "").strip()
            if not gid:
                continue
            region = _REGION_DIR.get((g.findtext("region") or "").strip(), "US")
            # The <locale> title is the clean one; the name= attribute carries "(USA) (EN)" noise.
            title = ""
            for loc in g.iter("locale"):
                title = (loc.findtext("title") or "").strip()
                if title:
                    break
            title = title or (g.get("name") or "").strip()
            if not title:
                continue
            year = None
            d = g.find("date")
            if d is not None and d.get("year"):
                try:
                    year = int(d.get("year"))
                except ValueError:
                    pass
            games.setdefault(_norm(title), []).append({
                "id": gid, "region": region, "title": title, "year": year,
                "console": self._console_of(gid, dump), "path": art_path,
            })
            n += 1
        return n

    def refresh(self) -> int:
        games: dict[str, list[dict]] = {}
        for dump, (url, art_path) in DUMPS.items():
            try:
                r = self._s.get(url, timeout=60)
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                n = self._parse(z.read(z.namelist()[0]), dump, art_path, games)
                log.info("gametdb: %s -> %d entries", dump, n)
            except Exception as exc:
                log.warning("gametdb: %s dump unavailable (%s)", dump, exc)

        if not games:
            return 0
        self._map = games
        try:
            self._path.write_text(json.dumps({"version": 2, "fetched": time.time(), "games": games}))
        except Exception as exc:
            log.warning("gametdb: could not cache dump (%s)", exc)
        log.info("gametdb: %d titles indexed", len(games))
        return len(games)

    # -- lookup --------------------------------------------------------------
    @staticmethod
    def serves(platform: str | None) -> bool:
        return (platform or "") in PLATFORMS

    def match_meta(self, meta: dict) -> dict | None:
        platform = meta.get("platform")
        if not self.serves(platform) or not self._map:
            return None
        want = PLATFORMS[platform]                       # wii | gamecube | wiiu
        rows = [r for r in self._map.get(_norm(meta.get("title") or ""), [])
                if r.get("console") == want]            # a Wii disc is not a GameCube disc
        if not rows:
            return None

        # Several regional pressings share a title. Prefer the one whose year agrees with the
        # sheet, then my region — a US disc for a US shelf.
        year = meta.get("year")

        def rank(r):
            same_year = 0 if (year and r.get("year") == year) else 1
            try:
                reg = _REGION_ORDER.index(r["region"])
            except ValueError:
                reg = len(_REGION_ORDER)
            return (same_year, reg)

        best = sorted(rows, key=rank)[0]
        gid, region, path = best["id"], best["region"], best["path"]
        return {
            "source": "GameTDB",
            "gameId": gid,
            "region": region,
            "console": want,
            "name": best["title"],
            "year": best.get("year"),
            # The printed face of the disc, the box front, and the full wrap — all straight
            # off their CDN. The front is region-correct art for a disc IGDB may never have
            # matched, so it earns its place in the cover chain, not just on the shelf.
            "disc": ART.format(path=path, kind="disc", region=region, gid=gid),
            "cover": ART.format(path=path, kind="cover", region=region, gid=gid),
            "coverFull": ART.format(path=path, kind="coverfullHQ", region=region, gid=gid),
            "url": f"https://www.gametdb.com/{'Wii' if want != 'wiiu' else 'WiiU'}/{gid}",
            "confidence": 10 if (year and best.get("year") == year) else 7,
        }
