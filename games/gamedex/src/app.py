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

from enrich import Enricher
from hltb import HltbClient
from igdb import IgdbClient
from metacritic import MetacriticClient
from poller import DataStore

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
enricher = (
    Enricher(_igdb, ENRICH_DB, backfill=ENRICH_BACKFILL, secondary=_secondary)
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
            "metacritic": enricher.get_secondary("metacritic", key)}


@app.get("/api/enrichment/stats")
def enrichment_stats():
    if not enricher:
        return {"enabled": False}
    return {"enabled": True, **enricher.stats()}


class Override(BaseModel):
    key: str
    url: str | None = None


@app.post("/api/enrichment/override")
def enrichment_override(body: Override):
    if not enricher:
        return JSONResponse(status_code=400, content={"error": "enrichment disabled"})
    if not body.url or not body.url.strip():          # clear → back to auto-match
        enricher.clear_override(body.key)
        return {"status": "pending", "detail": None}
    m = re.search(r"/games/([^/?#]+)", body.url)
    if not m:
        return JSONResponse(status_code=400, content={"error": "not an IGDB game URL (…/games/<slug>)"})
    result = _igdb.fetch_by_slug(m.group(1))
    if not result:
        return JSONResponse(status_code=404, content={"error": f"no IGDB game found for '{m.group(1)}'"})
    enrichment = _igdb.enrichment_from_result(result)
    enricher.set_override(body.key, enrichment)
    return {"status": "matched", "detail": {**enrichment, "manual": True}}


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
