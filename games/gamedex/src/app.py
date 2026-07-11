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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from enrich import Enricher
from igdb import IgdbClient
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

# Enricher is optional — only when IGDB creds are present.
_igdb = IgdbClient(IGDB_CLIENT_ID, IGDB_CLIENT_SECRET)
enricher = Enricher(_igdb, ENRICH_DB, backfill=ENRICH_BACKFILL) if _igdb.configured else None

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
    return {"enabled": True, "status": status, "detail": detail}


@app.get("/api/enrichment/stats")
def enrichment_stats():
    if not enricher:
        return {"enabled": False}
    return {"enabled": True, **enricher.stats()}


# Static UI last so /api/* wins. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
