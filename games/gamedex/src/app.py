"""FastAPI app: serves the normalized games data + the static browse UI, plus
lazy IGDB enrichment.

    GET  /api/data                 -> {meta, sheets:{games, completed, onOrder}} (gzipped)
    GET  /api/health               -> {status} (200 once first Dropbox load succeeds)
    POST /api/enrichment           -> {items, pending, stats} for a batch of matchKeys
    GET  /api/enrichment/detail    -> full IGDB detail for one matchKey
    GET  /api/enrichment/stats     -> enrichment progress
    GET  /                         -> static/index.html (+ /app.js, /style.css)

Enrichment is on-demand and host-cached (see enrich.py); it's disabled unless
IGDB credentials are configured.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import picross as picross_mod
import prefs as prefs_mod
import romm
import gamerankings as gr_mod
import shelf as shelf_mod

from arcadedb import ArcadeDbClient
from enrich import Enricher
from fallback import FallbackClient
from gameye import GameEyeClient
from gametdb import GameTdb
from hltb import HltbClient
from manuals import ManualClient
from igdb import IgdbClient
from metacritic import MetacriticClient
from poller import DataStore
import recommend
from cooptimus import CooptimusClient
from guides import GuideClient
from speedrun import SpeedrunClient
from steamx import SteamExtraClient
from thumby import ThumbyClient
from vgchartz import VgChartzClient
from vndb import VndbClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

DROPBOX_URL = os.environ.get("DROPBOX_XLSX_URL", "")
XLSX_FILENAME = os.environ.get("DROPBOX_XLSX_FILENAME", "Games Master List - Final.xlsx")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "600"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

IGDB_CLIENT_ID = os.environ.get("IGDB_CLIENT_ID", "")
IGDB_CLIENT_SECRET = os.environ.get("IGDB_CLIENT_SECRET", "")
ENRICH_DB = os.environ.get("ENRICH_DB", "/data/enrichment.sqlite")
ENRICH_BACKFILL = os.environ.get("ENRICH_BACKFILL", "false").lower() in ("1", "true", "yes")
_on = lambda name, default="true": os.environ.get(name, default).lower() in ("1", "true", "yes")

# Enricher is optional — only when IGDB creds are present. HLTB (playtimes) and
# Metacritic (critic scores) are secondary sources, on unless disabled.
_igdb = IgdbClient(IGDB_CLIENT_ID, IGDB_CLIENT_SECRET)
_secondary = {}
if _on("HLTB_ENABLED"):
    _secondary["hltb"] = HltbClient()
if _on("METACRITIC_ENABLED"):
    _secondary["metacritic"] = MetacriticClient()
if _on("GAMEEYE_ENABLED"):
    _secondary["gameye"] = GameEyeClient()
# Gated sources (see _SOURCE_GATE in enrich.py): each is only asked about the
# games it can actually speak to.
if _on("ARCADEDB_ENABLED"):
    _secondary["arcadedb"] = ArcadeDbClient()      # MAME romset -> cabinet art
if _on("VNDB_ENABLED"):
    _secondary["vndb"] = VndbClient()              # visual novels / adventure
if _on("THUMBY_ENABLED"):
    _secondary["thumby"] = ThumbyClient()          # Thumby / Thumby Color
if _on("VGCHARTZ_ENABLED"):
    _secondary["vgchartz"] = VgChartzClient()      # sales figures
if _on("STEAMX_ENABLED"):
    # Keyed on the Steam appid IGDB gives us — an exact lookup, no fuzzy match.
    _secondary["steamx"] = SteamExtraClient()      # Deck + ProtonDB + SteamSpy + achievements
if _on("SPEEDRUN_ENABLED"):
    _secondary["speedrun"] = SpeedrunClient()      # world-record times
if _on("GUIDES_ENABLED"):
    _secondary["guides"] = GuideClient()           # StrategyWiki walkthroughs
if _on("COOPTIMUS_ENABLED"):
    # IGDB says "co-operative"; Co-Optimus says how many, on one sofa or online.
    _secondary["cooptimus"] = CooptimusClient()
if _on("MANUALS_ENABLED"):
    # The instruction booklet. The one thing that was in the box that nobody keeps and no
    # games API carries — the Internet Archive has ~7,500 of them scanned.
    _secondary["manuals"] = ManualClient()
if _on("GAMETDB_ENABLED"):
    # The printed face of the disc, which is its own artwork and nothing else has it.
    _secondary["gametdb"] = GameTdb(os.environ.get("GAMETDB_DIR", "/data/gametdb"))
# Fallback metadata (IGN → Steam) for games IGDB doesn't match. GameSpot is
# off by default — its API is Cloudflare-blocked (see fallback.py).
_fallback = (
    FallbackClient(os.environ.get("GAMESPOT_API_KEY", ""), _on("GAMESPOT_ENABLED", "false"))
    if _on("FALLBACK_ENABLED") else None
)
enricher = (
    Enricher(_igdb, ENRICH_DB, backfill=ENRICH_BACKFILL, secondary=_secondary, fallback=_fallback)
    if _igdb.configured else None
)

store = DataStore(
    DROPBOX_URL, XLSX_FILENAME, REFRESH_INTERVAL,
    on_update=(enricher.reindex if enricher else None),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.start()
    # After the startup peak (xlsx parse + enrichment backfills), not during it.
    SHELF.warm(delay=90)
    if enricher:
        enricher.start()
        logging.getLogger("gamedex").info("IGDB enrichment enabled (backfill=%s)", ENRICH_BACKFILL)
    yield
    store.stop()
    if enricher:
        enricher.stop()


app = FastAPI(title="Gamedex", lifespan=lifespan)

# RomM: an exact (igdb_id, platform) -> rom id map, refreshed in the background.
# Only the mapping is ever served; the credentials stay here.
ROMM = romm.RommClient(
    base_url=os.getenv("ROMM_URL", ""),
    public_url=os.getenv("ROMM_PUBLIC_URL", ""),
    username=os.getenv("ROMM_USERNAME", ""),
    password=os.getenv("ROMM_PASSWORD", ""),
)
if ROMM.enabled:
    ROMM.start()


# Saved views + custom challenges, so they follow you between browsers.
PREFS = prefs_mod.Prefs(os.environ.get("PREFS_DB", "/data/prefs.sqlite"))


@app.get("/api/prefs")
def api_prefs_get():
    return {"prefs": PREFS.get_all()}


@app.put("/api/prefs/{key}")
async def api_prefs_put(key: str, request: Request):
    try:
        value = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    try:
        PREFS.put(key, value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.get("/api/romm")
def api_romm():
    """{"<igdb_id>|<platform folder>": rom_id} — the frontend turns a hit into
    <baseUrl>/console/rom/<id>/play. Empty (not an error) when RomM is off."""
    if not ROMM.enabled:
        return {"enabled": False, "roms": {}}
    return ROMM.snapshot()


# ---------- the shelf ----------

_resolved = {}
_rp = Path(os.environ.get("COVERS_RESOLVED", "/app/data/covers-resolved.json"))
if _rp.exists():
    import json as _json
    _resolved = _json.loads(_rp.read_text())
    logging.getLogger("gamedex").info(
        "shelf: %d real box wraps, %d spine colours",
        len(_resolved.get("wraps", {})), len(_resolved.get("hues", {})))
SHELF = shelf_mod.Shelf(_resolved, Path(os.environ.get("COVERS_CACHE", "/data/covers")))

# GameRankings: a frozen archive baked into the image, joined on (title, platform).
GR = gr_mod.GameRankings()


@app.get("/api/shelf")
def api_shelf():
    """The games you can physically pick up. Digital games are not objects and do not
    go on a shelf."""
    if not store.ready:
        return JSONResponse(status_code=503, content={"status": "loading", "games": []})
    snap = store.snapshot()
    enr = enricher.get_all_light() if enricher else {}
    rows = SHELF.rows(snap["data"]["games"]["rows"], enr)
    return {"games": rows, "wraps": sum(1 for r in rows if r["src"] == "wrap")}


@app.get("/api/shelf/face")
def api_shelf_face(key: str, face: str):
    """One face of one box, cut out of the real scan.

    Key is a QUERY param, not a path segment: a match key can contain '/' (platforms
    like 'Commodore Plus/4', 'OS/2', 'TI-99/4A'), and an encoded slash in a path is
    decoded by the proxy and re-splits the route — turning uploads into a 405 and box
    faces into 404s. In the query string it survives intact.

    Cut on first request and cached on disk from then on — we never hotlink their CDN,
    and a 6 MB scan is fetched exactly once in the lifetime of the volume."""
    img = SHELF.face(key, face)
    if img is None:
        return JSONResponse({"error": "no wrap"}, status_code=404)
    return Response(content=img, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _platform_for(key: str) -> str | None:
    """The platform of the game a box key belongs to. Key is '<matchkey>#<region>'.

    Searches EVERY sheet, because a game can live in any of them and box art must attach
    all the same. A game you've only beaten (an emulated one, say) has a row in Completed
    but not in Games; a game you've only ordered has a row in On Order and nowhere else —
    and that is exactly when you most want to give it a cover, since it has no metadata yet.
    Miss a sheet here and the upload 404s with the useless 'unknown game'."""
    mk = key.rsplit("#", 1)[0]
    data = store.snapshot()["data"]
    for sheet in ("games", "completed", "onOrder"):
        for r in data.get(sheet, {}).get("rows", []):
            if r.get("_k") == mk:
                return r.get("platform")
    return None


MAX_UPLOAD = 12 * 1024 * 1024


@app.post("/api/shelf/cover")
async def api_shelf_upload(request: Request, key: str, kind: str = "wrap", rotate: int = 0,
                          x1: float | None = None, x2: float | None = None,
                          w: float | None = None, h: float | None = None, d: float | None = None,
                          face_rot: int = 0,
                          cx1: float | None = None, cy1: float | None = None,
                          cx2: float | None = None, cy2: float | None = None):
    """Store a hand-supplied cover for one game — the image bytes are the raw POST body.

    The editor derives the box's proportions from the image (front aspect) and the user's
    dragged spine guides, and passes them here — w/h/d are the case dims in mm, x1/x2 the
    spine boundaries as fractions. This is the manual override: whatever you upload beats
    what we auto-resolved, which is how you fix a wrong match or a wrong box shape."""
    plat = _platform_for(key)
    if plat is None:
        return JSONResponse({"error": "unknown game"}, status_code=404)
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty body"}, status_code=400)
    if len(body) > MAX_UPLOAD:
        return JSONResponse({"error": "image too large (max 12 MB)"}, status_code=413)
    case = {"w": w, "h": h, "d": d} if None not in (w, h, d) else None
    # Front-only crop, as fractions of the rotated image. Absent → the whole image, which
    # is what every upload before this did.
    crop = ({"x1": cx1, "y1": cy1, "x2": cx2, "y2": cy2}
            if None not in (cx1, cy1, cx2, cy2) else None)
    try:
        entry = SHELF.set_cover(key, body, kind=kind, platform=plat, rotate=int(rotate),
                                x1=x1, x2=x2, case=case, face_rot=int(face_rot), crop=crop)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "src": "upload", "case": entry["case"], "v": entry["v"]}


@app.get("/api/gamerankings")
def api_gamerankings():
    """{matchKey: {score, n, url}} — the frozen GameRankings archive, joined to the sheet
    on (title, platform). A fallback critic score for games Metacritic never rated."""
    if not GR.enabled:
        return {}
    data = store.snapshot()["data"]
    rows = list(data.get("games", {}).get("rows", [])) + list(data.get("completed", {}).get("rows", []))
    return GR.for_rows(rows)


# ---- the daily Picross ----------------------------------------------------
PICROSS = picross_mod.Picross(os.environ.get("PICROSS_DIR", "/data/picross"))


def _picross_candidates() -> list[dict]:
    """Games worth waking up to. A cover you'd RECOGNISE, so: something I own or finished,
    with real box art — a 15x15 silhouette of a game I've never heard of is just a shape."""
    if not enricher:
        return []
    data = store.snapshot()["data"]
    light = enricher.get_all_light()          # {matchKey: {cover, ...}}
    out = []
    for r in data.get("games", {}).get("rows", []):
        if not (r.get("owned") or r.get("completed")):
            continue
        cover = (light.get(r.get("_k")) or {}).get("cover")
        if not cover:
            continue
        out.append({"key": r["_k"], "title": r.get("title"), "platform": r.get("platform"),
                    "year": r.get("releaseYear"), "cover": cover})
    return out


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@app.get("/api/picross/daily")
def api_picross_daily():
    """The clues, and nothing that spoils it — the solution stays on the server."""
    puz = PICROSS.daily(_today(), _picross_candidates())
    if not puz:
        return {"ok": False, "reason": "no puzzle today"}
    return {"ok": True, **picross_mod.Picross.public(puz)}


class PicrossSolve(BaseModel):
    grid: list[list[int]]


@app.post("/api/picross/solve")
def api_picross_solve(body: PicrossSolve):
    """Marking your own homework is no fun, so the answer never left the building. Send the
    grid; if it's right, you get the game you just drew."""
    puz = PICROSS.daily(_today(), _picross_candidates())
    if not puz:
        return {"ok": False}
    solved = body.grid == puz["grid"]
    return {"ok": True, "solved": solved, "game": puz["game"] if solved else None}


class PicrossGuess(BaseModel):
    title: str


@app.post("/api/picross/guess")
def api_picross_guess(body: PicrossGuess):
    """Name it before you finish it. Compared on the normalised title, so punctuation and
    case don't decide whether you were right."""
    puz = PICROSS.daily(_today(), _picross_candidates())
    if not puz:
        return {"ok": False}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    correct = norm(body.title) == norm(puz["game"]["title"])
    return {"ok": True, "correct": correct, "game": puz["game"] if correct else None}


@app.get("/api/uploads")
def api_uploads():
    """{matchKey: {url, v}} for every hand-uploaded cover, so the grid and drawer can
    show it as the game's art — including games that never matched IGDB."""
    return SHELF.uploaded_covers()


@app.delete("/api/shelf/cover")
def api_shelf_upload_delete(key: str):
    """Drop a manual upload, reverting to the auto-resolved cover."""
    return {"ok": SHELF.remove_cover(key)}


@app.get("/api/shelf/original")
def api_shelf_original(key: str):
    """The raw image the user uploaded — so the editor can reopen and re-adjust it."""
    got = SHELF.original(key)
    if got is None:
        return JSONResponse({"error": "no upload"}, status_code=404)
    data, ct = got
    return Response(content=data, media_type=ct,
                    headers={"Cache-Control": "no-store"})


app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/api/health")
def health():
    if store.ready:
        return {"status": "ok"}
    snap = store.snapshot()
    return JSONResponse(
        status_code=503,
        content={"status": "loading", "lastError": snap["meta"]["lastError"]},
    )


@app.get("/api/data")
def data():
    snap = store.snapshot()
    if not store.ready:
        return JSONResponse(
            status_code=503,
            content={"status": "loading", "meta": snap["meta"], "sheets": {}},
        )
    meta = dict(snap["meta"])
    meta["enrichment"] = enricher.stats() if enricher else {"enabled": False}
    return {"meta": meta, "sheets": snap["data"]}


class KeyBatch(BaseModel):
    keys: list[str]


@app.post("/api/enrichment")
def enrichment_batch(batch: KeyBatch):
    if not enricher:
        return {"enabled": False, "items": {}, "pending": []}
    enricher.request(batch.keys)               # queue the on-screen ones first
    items, pending = enricher.get_light(batch.keys)
    return {"enabled": True, "items": items, "pending": pending, "stats": enricher.stats()}


@app.get("/api/enrichment/all")
def enrichment_all():
    if not enricher:
        return {"enabled": False, "items": {}}
    return {
        "enabled": True,
        "items": enricher.get_all_light(),
        # Games we looked up and found nothing for. Without this the UI cannot
        # distinguish "still resolving" from "resolved, no metadata", and shows a
        # loading skeleton forever on anything unmatchable.
        "noMatch": enricher.resolved_no_match(),
        "stats": enricher.stats(),
    }


@app.get("/api/enrichment/detail")
def enrichment_detail(key: str):
    if not enricher:
        return {"enabled": False, "status": "disabled", "detail": None}
    status, detail = enricher.get_detail(key)
    return {"enabled": True, "status": status, "detail": detail,
            "hltb": enricher.get_secondary("hltb", key),
            "metacritic": enricher.get_secondary("metacritic", key),
            "gameye": enricher.get_secondary("gameye", key),
            "arcadedb": enricher.get_secondary("arcadedb", key),
            "vndb": enricher.get_secondary("vndb", key),
            "vgchartz": enricher.get_secondary("vgchartz", key),
            "thumby": enricher.get_secondary("thumby", key),
            "steamx": enricher.get_secondary("steamx", key),
            "speedrun": enricher.get_secondary("speedrun", key),
            "guides": enricher.get_secondary("guides", key),
            "cooptimus": enricher.get_secondary("cooptimus", key)}


@app.get("/api/enrichment/stats")
def enrichment_stats():
    if not enricher:
        return {"enabled": False}
    return {"enabled": True, **enricher.stats()}


class Override(BaseModel):
    key: str
    url: str | None = None
    source: str = "igdb"
    remove: bool = False      # pin as "no match" instead of re-auto-matching


# IGN/GameSpot/Steam supply the *primary* metadata record, so mapping them
# writes to the same slot as IGDB.
_PRIMARY_FALLBACKS = ("ign", "steam", "gamespot", "launchbox", "keitai")


@app.post("/api/enrichment/override")
def enrichment_override(body: Override):
    if not enricher:
        return JSONResponse(status_code=400, content={"error": "enrichment disabled"})
    src = body.source or "igdb"
    # A primary-metadata source (igdb or a fallback) occupies the igdb slot.
    slot = "igdb" if (src == "igdb" or src in _PRIMARY_FALLBACKS) else src

    if body.remove:                                   # pin as no match, don't re-match
        enricher.remove_source(slot, body.key)
        return {"status": "removed", "source": src}

    if src == "igdb":
        client = _igdb
    elif src in _PRIMARY_FALLBACKS:
        client = _fallback.client_for(src) if _fallback else None
    else:
        client = _secondary.get(src)
    if client is None or not hasattr(client, "override_from_url"):
        return JSONResponse(status_code=400, content={"error": f"source '{src}' can't be mapped"})

    if not body.url or not body.url.strip():          # clear → back to auto
        enricher.clear_source_override(slot, body.key)
        return {"status": "cleared", "source": src}
    meta = enricher.meta_for(body.key) or {}
    record = client.override_from_url(meta.get("title", ""), body.url)
    if not record:
        return JSONResponse(status_code=404, content={"error": "no match found for that URL"})
    enricher.set_source_override(slot, body.key, record)
    return {"status": "matched", "source": src, "record": record}


# Cached against the workbook hash: the answer only changes when the sheet does
# (or when enrichment fills in more similar-games lists, which the count catches).
_recs = {"hash": None, "matched": -1, "data": None}


@app.get("/api/recommendations")
def recommendations():
    """"Because you liked …" — see recommend.py."""
    if not enricher or not store.ready:
        return {"enabled": False, "items": []}
    snap = store.snapshot()
    src_hash = snap["meta"].get("sourceHash")
    matched = enricher.stats().get("matched", 0)
    if _recs["data"] and _recs["hash"] == src_hash and _recs["matched"] == matched:
        return {"enabled": True, **_recs["data"]}
    data = recommend.build(
        snap["data"].get("games", {}).get("rows", []),
        enricher.all_records(),
        enricher.normalize,
    )
    _recs.update({"hash": src_hash, "matched": matched, "data": data})
    return {"enabled": True, **data}


@app.get("/api/value-history")
def value_history():
    """Daily snapshots of the collection's total value (see enrich.snapshot_value)."""
    if not enricher:
        return {"enabled": False, "history": []}
    return {"enabled": True, "history": enricher.value_history()}


@app.post("/api/refresh")
def refresh():
    """Eagerly re-check Dropbox now (instead of waiting for the poll interval)."""
    try:
        changed = store.refresh_once()
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})
    meta = dict(store.snapshot()["meta"])
    meta["enrichment"] = enricher.stats() if enricher else {"enabled": False}
    return {"changed": changed, "meta": meta}


# Static UI last so /api/* wins. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
