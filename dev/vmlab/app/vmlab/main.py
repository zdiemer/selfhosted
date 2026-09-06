"""vmlab — a browser VM picker for short-lived guests.

Routes are `def` (not `async def`) wherever they call into k8s.py, so Starlette
runs them in its threadpool; the official Kubernetes client is synchronous and a
blocking call in an async route would stall the console proxy for every session.
The proxy routes are the exception and are genuinely async.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from vmlab import k8s, proxy, snapshots, spec
from vmlab.config import settings

logger = logging.getLogger("vmlab")

STATIC = Path(__file__).parent / "static"


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


async def _reaper() -> None:
    """Cull sessions nobody is watching.

    Deliberately the *soft* half of the TTL story. The hard cap is
    activeDeadlineSeconds on the pod, enforced by the kubelet, so a session
    still dies on time if this task — or this whole Deployment — is down.
    """
    while True:
        try:
            await asyncio.sleep(60)
            culled = await asyncio.to_thread(k8s.reap_idle)
            if culled:
                logger.info("reaped %d idle session(s)", culled)
            # Separate from the idle cull: a pod that already exited is not a
            # "session" any more, but it still holds quota and its disk.
            gone = await asyncio.to_thread(k8s.reap_terminal)
            if gone:
                logger.info("removed %d finished session pod(s)", gone)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reaper iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    k8s.init()
    logger.info(
        "vmlab up: namespace=%s max_concurrent=%d idle_timeout=%ds profiles=%s",
        settings.namespace,
        settings.max_concurrent_vms,
        settings.idle_timeout_seconds,
        ",".join(sorted(settings.network_profiles)),
    )
    task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await proxy.aclose()


app = FastAPI(title="vmlab", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(snapshots.SnapshotError)
async def _snapshot_handler(_: Request, exc: snapshots.SnapshotError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(spec.ValidationError)
async def _validation_handler(_: Request, exc: spec.ValidationError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    # Touches nothing external, on purpose: a probe that called the API server
    # would let a slow API server kill this pod, taking the reaper with it —
    # exactly when the cluster is already unhappy.
    return {"ok": True}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@app.get("/api/catalog")
def api_catalog() -> dict:
    configs = k8s.list_configs()
    sessions = {s["slug"]: s for s in k8s.list_sessions()}
    out = []
    for cfg in configs:
        out.append(
            {
                **cfg,
                "iso_status": k8s.iso_status(cfg["slug"]) if cfg.get("iso") else "absent",
                "hasIsoUrl": bool(cfg.get("iso")),
                "session": sessions.get(cfg["slug"]),
                # Two distinct things that were briefly one: whether this config
                # CAN take save points (a bool from values.yaml) and which ones
                # it HAS (a list). Overwriting the former with the latter made
                # the UI's "can I show a Save button" test silently undefined.
                "savepointsEnabled": bool(cfg.get("savepoints")),
                "savepoints": k8s.list_savepoints(cfg["slug"]),
            }
        )
    return {
        "configs": out,
        "limits": {
            "maxConcurrent": settings.max_concurrent_vms,
            "maxCores": settings.max_cores,
            "maxMemoryMib": settings.max_memory_mib,
            "maxDiskGib": settings.max_disk_gib,
            "idleTimeoutSeconds": settings.idle_timeout_seconds,
            "networkProfiles": sorted(settings.network_profiles),
        },
        "running": len([s for s in sessions.values() if s["phase"] in {"Pending", "Running"}]),
    }


@app.post("/api/catalog")
def api_create_config(body: dict = Body(...)) -> dict:
    return k8s.create_config(body)


@app.delete("/api/catalog/{slug}")
def api_delete_config(slug: str) -> JSONResponse:
    for cfg in k8s.list_configs():
        if cfg["slug"] == slug and cfg.get("source") == "seed":
            # Helm would recreate it on the next upgrade, so a "success" here
            # would be a lie that resolves itself confusingly later.
            return JSONResponse(
                {"error": "this config comes from values.yaml — remove it there"},
                status_code=400,
            )
    if not k8s.delete_config(slug):
        return JSONResponse({"error": "no such config"}, status_code=404)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# ISOs
# ---------------------------------------------------------------------------


@app.post("/api/isos/{slug}/fetch")
def api_fetch_iso(slug: str) -> dict:
    k8s.clear_failed_fetches(slug)
    return {"job": k8s.start_fetch(slug)}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@app.get("/api/sessions")
def api_sessions() -> dict:
    return {"sessions": k8s.list_sessions()}


@app.post("/api/sessions")
def api_launch(body: dict = Body(...)) -> dict:
    slug = (body.get("slug") or "").strip()
    boot_from_iso = body.get("bootFromIso")
    return k8s.launch(
        slug,
        boot_from_iso=None if boot_from_iso is None else bool(boot_from_iso),
        ttl_seconds=body.get("ttlSeconds"),
        load_snapshot=(body.get("loadSnapshot") or None),
    )


@app.delete("/api/sessions/{session_id}")
def api_stop(session_id: str) -> JSONResponse:
    if not k8s.stop(session_id):
        return JSONResponse({"error": "no such session"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/api/sessions/{session_id}/log")
def api_log(session_id: str) -> dict:
    return {"log": k8s.session_log(session_id)}


# ---------------------------------------------------------------------------
# Save points
# ---------------------------------------------------------------------------


@app.get("/api/savepoints/{slug}")
def api_savepoints(slug: str) -> dict:
    return {"savepoints": k8s.list_savepoints(slug)}


@app.post("/api/savepoints/{slug}")
def api_create_savepoint(slug: str, body: dict = Body(...)) -> dict:
    # Saving stops the guest, snapshots RAM and disk, then resumes. On a large
    # guest that is seconds, not milliseconds, so it runs in the threadpool
    # like every other blocking call here.
    return k8s.create_savepoint(slug, body.get("name", ""))


@app.delete("/api/savepoints/{slug}/{name}")
def api_delete_savepoint(slug: str, name: str) -> JSONResponse:
    k8s.delete_savepoint(slug, name)
    return JSONResponse({"ok": True})


@app.get("/api/savepoints/{slug}/{name}/screenshot.png")
def api_savepoint_screenshot(slug: str, name: str, full: int = 0):
    # Thumbnail by default: the catalog shows these at 148px and polls, so
    # serving the 1.3MB original would make a wall of save points cost tens of
    # megabytes per refresh. ?full=1 is the click-through.
    path = k8s.savepoint_screenshot(slug, name, full=bool(full))
    if path is None:
        return JSONResponse({"error": "no screenshot"}, status_code=404)
    # Immutable once written — the name is unique per save point and a new save
    # under the same name replaces the file, so a short cache is safe and keeps
    # a wall of thumbnails from re-fetching on every poll.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=60"})


# ---------------------------------------------------------------------------
# Console proxy
# ---------------------------------------------------------------------------


@app.websocket("/vm/{session_id}/{path:path}")
async def ws_console(websocket: WebSocket, session_id: str, path: str) -> None:
    await proxy.forward_ws(session_id, path, websocket)


@app.get("/vm/{session_id}")
async def console_slash(session_id: str) -> RedirectResponse:
    # Not cosmetic. Without the trailing slash the browser resolves noVNC's
    # relative assets — and its `websockify` socket — against /vm/ instead of
    # /vm/<sid>/, and the console silently fails to connect.
    return RedirectResponse(url=f"/vm/{session_id}/", status_code=307)


@app.api_route("/vm/{session_id}/{path:path}", methods=["GET", "POST", "HEAD"])
async def console(session_id: str, path: str, request: Request):
    return await proxy.forward_http(session_id, path, request)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def run() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("VMLAB_PORT_NUMBER", "8080")),
        log_config=None,
        # noVNC sends large framebuffer updates; the default cap rejects them.
        ws_max_size=16 * 1024 * 1024,
    )


if __name__ == "__main__":
    run()
