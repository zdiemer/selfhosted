from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
CATALOG = Path(os.environ.get("ARCHIVE_CATALOG", "/work/library.sqlite3"))
ARCHIVE_ROOT = Path(os.environ.get("ARCHIVE_ROOT", "/archive"))
PAGE_SIZE = 30
SOURCE_LABELS = {
    "nsider2": "NSider2",
    "indienerds": "IndieNerds",
    "photobucket": "PhotoBucket",
}

app = FastAPI(
    title="Early Internet Archive",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; img-src 'self' data:; style-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def connect() -> sqlite3.Connection:
    if not CATALOG.is_file():
        raise HTTPException(status_code=503, detail="archive catalog is not mounted")
    db = sqlite3.connect(f"file:{CATALOG}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def decode_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.pop("rank", None)
    item["tags"] = json.loads(item.pop("tags_json"))
    item["metadata"] = json.loads(item.pop("metadata_json"))
    item["source_label"] = SOURCE_LABELS.get(item["source"], item["source"])
    item["date"] = display_date(item["published_at"])
    item["canonical_url"] = safe_external_url(item["canonical_url"])
    return item


def display_date(value: str) -> str:
    if not value:
        return "Unknown date"
    if re.fullmatch(r"\d{14}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value[:10]


def safe_external_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def fts_query(value: str) -> str:
    terms = re.findall(r"[\w'-]+", value, flags=re.UNICODE)[:12]
    return " AND ".join(f'"{term.replace(chr(34), "")}"*' for term in terms)


def pagination_url(request: Request, page: int) -> str:
    params = dict(request.query_params)
    params["page"] = str(page)
    return f"{request.url.path}?{urlencode(params)}"


templates.env.globals.update(
    display_date=display_date,
    pagination_url=pagination_url,
    source_labels=SOURCE_LABELS,
)


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    if not CATALOG.is_file():
        return JSONResponse({"status": "catalog-missing"}, status_code=503)
    try:
        with closing(connect()) as db:
            db.execute("SELECT 1 FROM metadata LIMIT 1").fetchone()
        return JSONResponse({"status": "ok"})
    except (sqlite3.Error, HTTPException) as exc:
        return JSONResponse({"status": "catalog-error", "detail": str(exc)}, status_code=503)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with closing(connect()) as db:
        stats = [dict(row) for row in db.execute("SELECT * FROM source_stats ORDER BY first_at,source")]
        totals = {
            "items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "context": db.execute("SELECT COUNT(*) FROM context_messages").fetchone()[0],
            "assets": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        }
        latest = [decode_item(row) for row in db.execute("SELECT * FROM items ORDER BY published_at DESC LIMIT 8")]
        generated = json.loads(db.execute("SELECT value FROM metadata WHERE key='generated_at'").fetchone()[0])
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={"stats": stats, "totals": totals, "latest": latest, "generated": generated},
    )


def run_search(db: sqlite3.Connection, q: str, source: str, year: str, page: int) -> tuple[list[dict], int]:
    joins = ""
    where = []
    params: list[object] = []
    if q.strip():
        expression = fts_query(q)
        if not expression:
            return [], 0
        joins = "JOIN items_fts f ON f.id=i.id"
        where.append("items_fts MATCH ?")
        params.append(expression)
    if source:
        where.append("i.source=?")
        params.append(source)
    if year:
        where.append("substr(i.published_at,1,4)=?")
        params.append(year)
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM items i {joins}{clause}", params).fetchone()[0]
    rank = ", bm25(items_fts) AS rank" if joins else ""
    order = "rank, i.published_at DESC" if joins else "i.published_at DESC"
    rows = db.execute(
        f"SELECT i.*{rank} FROM items i {joins}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    return [decode_item(row) for row in rows], total


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    source: str = "",
    year: str = "",
    page: int = Query(1, ge=1),
):
    with closing(connect()) as db:
        items, total = run_search(db, q, source, year, page)
        sources = [dict(row) for row in db.execute("SELECT * FROM source_stats WHERE item_count>0 ORDER BY label")]
        years = [row[0] for row in db.execute("SELECT DISTINCT substr(published_at,1,4) FROM items WHERE published_at!='' ORDER BY 1")]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        raise HTTPException(status_code=404, detail="page does not exist")
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "items": items, "total": total, "q": q, "source": source, "year": year,
            "page": page, "pages": pages, "sources": sources, "years": years,
        },
    )


@app.get("/item/{source}/{external_id}", response_class=HTMLResponse)
def item_detail(request: Request, source: str, external_id: str):
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM items WHERE source=? AND external_id=?", (source, external_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="archive item not found")
        item = decode_item(row)
        context = [
            dict(message)
            for message in db.execute(
                "SELECT * FROM context_messages WHERE item_id=? ORDER BY sequence", (item["id"],)
            )
        ]
        assets = [
            dict(asset)
            for asset in db.execute(
                "SELECT * FROM assets WHERE EXISTS (SELECT 1 FROM json_each(item_ids_json) WHERE value=?) ORDER BY id",
                (item["id"],),
            )
        ]
    return templates.TemplateResponse(
        request=request,
        name="item.html",
        context={"item": item, "context": context, "assets": assets},
    )


@app.get("/gallery", response_class=HTMLResponse)
def gallery(request: Request, source: str = "", page: int = Query(1, ge=1)):
    where = "WHERE source=?" if source else ""
    params = (source,) if source else ()
    with closing(connect()) as db:
        total = db.execute(f"SELECT COUNT(*) FROM assets {where}", params).fetchone()[0]
        assets = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM assets {where} ORDER BY captured_at,id LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            )
        ]
        sources = [dict(row) for row in db.execute("SELECT * FROM source_stats WHERE asset_count>0 ORDER BY label")]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        raise HTTPException(status_code=404, detail="page does not exist")
    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={"assets": assets, "total": total, "source": source, "sources": sources, "page": page, "pages": pages},
    )


@app.get("/asset/{asset_id:path}")
def asset(asset_id: str):
    with closing(connect()) as db:
        row = db.execute("SELECT media_path,mime_type FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="asset not found")
    root = ARCHIVE_ROOT.resolve()
    target = (root / row["media_path"]).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="asset payload not found")
    media_type = row["mime_type"] or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/v1/search")
def search_api(q: str = "", source: str = "", year: str = "", page: int = Query(1, ge=1)):
    with closing(connect()) as db:
        items, total = run_search(db, q, source, year, page)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        raise HTTPException(status_code=404, detail="page does not exist")
    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": PAGE_SIZE,
    }
