"""GameRankings — a frozen fallback critic score.

GameRankings closed in 2019. Its scores are the ones the sheet's own critic column was
built from for older games, and they cover a stretch of the 90s/2000s that Metacritic
never rated. Nothing here talks to the network: tools/gamerankings.py bakes the whole
archive into data/gamerankings.json and we just look games up in it.

The lookup is (normalised title, platform) — GameRankings has no IGDB id to join on, and
a title+platform pair is specific enough that a false hit needs two games with the same
name on the same machine.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re

log = logging.getLogger("gamedex.gamerankings")

_PATH = pathlib.Path(os.environ.get("GAMERANKINGS_DATA", "/app/data/gamerankings.json"))
ARCHIVE = "https://gr.blade.sk"          # the community mirror; gamerankings.com is dead


def _norm(t: str) -> str:
    """Title -> comparison key. Must stay in step with tools/gamerankings.py."""
    t = (t or "").lower().replace("’", "").replace("'", "")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return re.sub(r"\s+", " ", t)


class GameRankings:
    def __init__(self, path: pathlib.Path = _PATH):
        self._idx: dict[str, list] = {}
        try:
            self._idx = json.loads(path.read_text())
            log.info("gamerankings: %d entries loaded", len(self._idx))
        except Exception as e:                       # a missing bake must never break boot
            log.warning("gamerankings: no data (%s)", e)

    @property
    def enabled(self) -> bool:
        return bool(self._idx)

    def lookup(self, title: str, platform: str) -> dict | None:
        hit = self._idx.get(f"{_norm(title)}|{(platform or '').strip()}")
        if not hit:
            return None
        score, n, path = hit
        return {"score": score, "n": n,
                "url": f"{ARCHIVE}/{path}" if path else ARCHIVE}

    def for_rows(self, rows) -> dict:
        """{matchKey: {score, n, url}} for every row we have a score for."""
        out = {}
        for r in rows:
            mk = r.get("_k")
            if not mk or mk in out:
                continue
            got = self.lookup(r.get("title") or r.get("game"), r.get("platform"))
            if got:
                out[mk] = got
        return out
