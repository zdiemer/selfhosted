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

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import romm

from arcadedb import ArcadeDbClient
from enrich import Enricher
from fallback import FallbackClient
from gameye import GameEyeClient
from hltb import HltbClient
from igdb import IgdbClient
from metacritic import MetacriticClient
from poller import DataStore
import recommend
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


@app.get("/api/romm")
def api_romm():
    """{"<igdb_id>|<platform folder>": rom_id} — the frontend turns a hit into
    <baseUrl>/console/rom/<id>/play. Empty (not an error) when RomM is off."""
    if not ROMM.enabled:
        return {"enabled": False, "roms": {}}
    return ROMM.snapshot()

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
    return {"enabled": True, "items": enricher.get_all_light(), "stats": enricher.stats()}


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
            "guides": enricher.get_secondary("guides", key)}


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
_PRIMARY_FALLBACKS = ("ign", "steam", "gamespot", "launchbox")


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
