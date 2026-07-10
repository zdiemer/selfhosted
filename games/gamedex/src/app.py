"""FastAPI app: serves the normalized games data + the static browse UI.

The whole dataset (~17k rows) is small enough to ship to the browser once and
facet/search entirely client-side, so the API surface is tiny:

    GET /api/data    -> {meta, sheets:{games, completed, onOrder}}  (gzipped)
    GET /api/health  -> {status} (200 once the first Dropbox load succeeds)
    GET /            -> static/index.html  (+ /app.js, /style.css)
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

from poller import DataStore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

DROPBOX_URL = os.environ.get("DROPBOX_XLSX_URL", "")
XLSX_FILENAME = os.environ.get("DROPBOX_XLSX_FILENAME", "Games Master List - Final.xlsx")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "600"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

store = DataStore(DROPBOX_URL, XLSX_FILENAME, REFRESH_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.start()
    yield
    store.stop()


app = FastAPI(title="Gamedex", lifespan=lifespan)
# Compress the data payload (the 14.7k-row sheet is a few MB raw, ~1MB gzipped).
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
    return {"meta": snap["meta"], "sheets": snap["data"]}


# Static UI last so /api/* wins. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
