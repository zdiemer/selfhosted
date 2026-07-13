"""The daily Picross — a nonogram cut from a game's own box art.

One puzzle a day, generated from a cover in the library. Solve the grid and the picture you
uncover IS the answer; guess the game before you finish and you get the bonus.

Two things make this harder than "threshold an image and print the clues":

  IT HAS TO BE SOLVABLE BY LOGIC. An arbitrary picture makes an arbitrary grid, and most
  arbitrary grids are not deducible — you reach a point where two different fillings satisfy
  every clue and the only way on is to guess. That isn't a nonogram, it's a colouring book.
  So every candidate is run through a line solver (the same constraint propagation a human
  does: intersect all placements a line's clues allow, repeat until nothing changes) and
  thrown away unless the solver can finish it unaided.

  IT HAS TO LOOK LIKE SOMETHING. A cover reduced to 15x15 black and white is mostly mush.
  We try several thresholds per cover, keep the fill fraction in a band, and reject grids
  that are too sparse, too dense, or too noisy (a grid where every row is a dozen one-cell
  runs is technically solvable and miserable to look at).

Candidates are tried in a deterministic order seeded by the date, so everyone loading the
page on the same day gets the same puzzle, and the result is cached on the PVC.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import pathlib
import random

import requests
from PIL import Image, ImageOps

log = logging.getLogger("gamedex.picross")

W, H = 15, 15                    # a few minutes' solve; bigger is a chore on a phone
FILL_LO, FILL_HI = 0.36, 0.60    # how much of the grid is filled in
MAX_RUNS_PER_LINE = 5            # a line of eight one-cell runs is noise, not a picture
CANDIDATES = 60                  # covers to try before giving up on the day
IGDB_IMG = "https://images.igdb.com/igdb/image/upload/t_cover_big/{}.jpg"


# ---- clues ----------------------------------------------------------------
def _runs(line: list[int]) -> list[int]:
    """The clue for one line: the lengths of its filled runs."""
    out, n = [], 0
    for c in line:
        if c:
            n += 1
        elif n:
            out.append(n)
            n = 0
    if n:
        out.append(n)
    return out or [0]


def clues_of(grid: list[list[int]]) -> tuple[list, list]:
    rows = [_runs(r) for r in grid]
    cols = [_runs([grid[y][x] for y in range(len(grid))]) for x in range(len(grid[0]))]
    return rows, cols


# ---- the solver ------------------------------------------------------------
# A cell is 1 (filled), 0 (empty) or None (unknown). This is exactly what a person does:
# for one line, work out every arrangement its clue permits given what's already known, and
# keep only the cells that agree in all of them. Repeat over rows and columns until a pass
# changes nothing. If the grid comes out complete, the puzzle is deducible without guessing.
def _placements(clue: list[int], known: list, length: int):
    """Every filling of `length` cells matching `clue` and consistent with `known`."""
    if clue == [0]:
        clue = []
    out = []

    def walk(i: int, ci: int, acc: list):
        if len(out) > 20000:                      # pathological line; treat as unsolvable
            return
        if ci == len(clue):
            rest = acc + [0] * (length - len(acc))
            if all(k is None or k == v for k, v in zip(known, rest)):
                out.append(rest)
            return
        run = clue[ci]
        # earliest start .. latest start for this run
        need = sum(clue[ci:]) + (len(clue) - ci - 1)
        for start in range(i, length - need + 1):
            cand = acc + [0] * (start - len(acc)) + [1] * run
            if len(cand) > length:
                break
            if ci < len(clue) - 1:
                cand = cand + [0]                 # mandatory gap between runs
            if len(cand) > length:
                break
            if any(k is not None and k != v for k, v in zip(known[:len(cand)], cand)):
                continue
            walk(len(cand), ci + 1, cand)

    walk(0, 0, [])
    return out


def _tighten(clue: list[int], known: list, length: int):
    """The cells every legal placement agrees on. None if the line is impossible."""
    ps = _placements(clue, known, length)
    if not ps:
        return None
    out = []
    for i in range(length):
        vals = {p[i] for p in ps}
        out.append(vals.pop() if len(vals) == 1 else None)
    return out


def line_solvable(rows: list, cols: list, w: int, h: int) -> bool:
    """Can constraint propagation alone finish this puzzle? (No guessing allowed.)"""
    grid = [[None] * w for _ in range(h)]
    for _ in range(40):
        before = sum(c is not None for r in grid for c in r)
        for y in range(h):
            t = _tighten(rows[y], grid[y], w)
            if t is None:
                return False
            grid[y] = t
        for x in range(w):
            col = [grid[y][x] for y in range(h)]
            t = _tighten(cols[x], col, h)
            if t is None:
                return False
            for y in range(h):
                grid[y][x] = t[y]
        after = sum(c is not None for r in grid for c in r)
        if after == before:
            break
    return all(c is not None for r in grid for c in r)


# ---- turning a cover into a grid -------------------------------------------
def _grid_from_image(im: Image.Image, cutoff: float) -> list[list[int]]:
    """Square-crop, shrink to W x H, and split light from dark at a percentile."""
    im = ImageOps.exif_transpose(im).convert("L")
    # Crop to a square from the middle of the cover — box art puts the logo and the
    # character in the middle, and the top/bottom are usually a platform banner.
    s = min(im.size)
    left = (im.width - s) // 2
    top = int((im.height - s) * 0.40)             # bias UP: the title is usually high
    im = im.crop((left, top, left + s, top + s))
    im = ImageOps.autocontrast(im.resize((W, H), Image.LANCZOS))
    px = list(im.getdata())
    thresh = sorted(px)[min(len(px) - 1, int(len(px) * cutoff))]
    # Dark cells are "filled" — a game's art is usually a bright subject on a dark field, and
    # the silhouette reads better than its negative.
    return [[1 if px[y * W + x] <= thresh else 0 for x in range(W)] for y in range(H)]


def _decent(grid) -> bool:
    fill = sum(sum(r) for r in grid) / (W * H)
    if not (FILL_LO <= fill <= FILL_HI):
        return False
    rows, cols = clues_of(grid)
    # Reject confetti: a line broken into many tiny runs is unpleasant to solve and
    # unreadable as a picture.
    if max(len(r) for r in rows) > MAX_RUNS_PER_LINE:
        return False
    if max(len(c) for c in cols) > MAX_RUNS_PER_LINE:
        return False
    if sum(1 for r in rows if r == [0]) > 2 or sum(1 for c in cols if c == [0]) > 2:
        return False                              # blank lines are dead space
    return True


class Picross:
    def __init__(self, cache_dir: str = "/data/picross"):
        self._dir = pathlib.Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._s = requests.Session()

    def _cover(self, image_id: str) -> Image.Image | None:
        try:
            r = self._s.get(IGDB_IMG.format(image_id), timeout=20)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content))
        except Exception as exc:
            log.debug("picross: cover %s unavailable (%s)", image_id, exc)
            return None

    def daily(self, date: str, candidates: list[dict]) -> dict | None:
        """The puzzle for `date`. `candidates` is [{key,title,platform,year,cover}, ...].

        Cached: the first request of the day builds it, everyone else reads the file."""
        path = self._dir / f"{date}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        if not candidates:
            return None

        # Deterministic shuffle: same day, same order, same puzzle for everyone.
        seed = int(hashlib.sha256(date.encode()).hexdigest()[:12], 16)
        pool = list(candidates)
        random.Random(seed).shuffle(pool)

        for g in pool[:CANDIDATES]:
            im = self._cover(g["cover"])
            if im is None:
                continue
            for cutoff in (0.44, 0.50, 0.38, 0.56):
                grid = _grid_from_image(im, cutoff)
                if not _decent(grid):
                    continue
                rows, cols = clues_of(grid)
                if not line_solvable(rows, cols, W, H):
                    continue                       # deducible, or it isn't a nonogram
                out = {
                    "date": date, "w": W, "h": H, "rows": rows, "cols": cols,
                    "grid": grid,                  # the answer; never sent to the browser
                    "game": {"key": g["key"], "title": g["title"], "platform": g.get("platform"),
                             "year": g.get("year"), "cover": g["cover"]},
                }
                path.write_text(json.dumps(out))
                log.info("picross %s: %s (%s)", date, g["title"], g.get("platform"))
                return out
        log.warning("picross %s: no cover produced a solvable grid", date)
        return None

    @staticmethod
    def public(puz: dict) -> dict:
        """What the browser is allowed to see: the clues, and nothing that spoils it."""
        return {"date": puz["date"], "w": puz["w"], "h": puz["h"],
                "rows": puz["rows"], "cols": puz["cols"]}
