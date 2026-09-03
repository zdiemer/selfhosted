"""hatch — a read-mostly cluster API for an external agent.

Startup order matters here. The API-key check runs FIRST and exits non-zero if
it fails, so a keyless release CrashLoops without ever answering a request.
That check lives here rather than in a helm `required`, because a chart that
cannot render from tracked values drops out of scripts/ci-lint-charts.sh AND,
silently, out of scripts/ci-lint-availability.sh — and this is the chart with
the PDB, the spread constraint and the preStop hook that lint exists to check.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hatch_api import grafana, k8s, logging_setup
from hatch_api.config import settings
from hatch_api.routes import actions, cluster, health, query, resources

logger = logging.getLogger("hatch")


def _preflight() -> None:
    for err in settings.key_errors:
        logger.error("configuration error: %s", err)
    if not settings.api_keys:
        logger.error(
            "HATCH_API_KEYS is empty or unparseable — refusing to serve an "
            "unauthenticated cluster API. Set secrets.apiKeys in values.local.yaml."
        )
        sys.exit(1)
    logger.info(
        "loaded %d api key(s): %s",
        len(settings.api_keys),
        ", ".join(f"{k.name}[{','.join(sorted(k.scopes))}]" for k in settings.api_keys),
    )
    if settings.actions_enabled:
        logger.info(
            "actions ENABLED (rbac.act.mode=%s, allowNamespaces=%s); deny: %s",
            settings.rbac_act_mode,
            sorted(settings.rbac_act_allow_namespaces),
            settings.actions_deny_workloads,
        )
    else:
        logger.info("actions disabled; no mutation RBAC is bound")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging_setup.configure()
    _preflight()
    k8s.init()
    # Resolve datasource UIDs once. A failure degrades the query routes to 503
    # with a useful message; it never fails the pod, because losing history
    # should not also lose /v1/pods.
    await grafana.verify()
    yield


app = FastAPI(
    title="hatch",
    version="v1",
    summary="Read-mostly cluster API for an external agent on the tailnet",
    description=(
        "Bearer-token authenticated. Send the key as `Authorization: Bearer <key>` "
        "or `X-API-Key` — NEVER as a query parameter, because Traefik's access log "
        "records the query string and ships it off-cluster.\n\n"
        "Start with `GET /v1/cluster/summary` (a few KB). `GET /v1/cluster/status` "
        "is the full ~165KB collector document and takes a `?section=` projection. "
        "`GET /v1/actions` reports which mutations are permitted before you try one."
    ),
    lifespan=lifespan,
    # An agent does not need HTML, and these would be two more unauthenticated
    # routes to reason about. /openapi.json stays: it is how the surface is
    # discovered, and the host is tailnet-only.
    docs_url=None,
    redoc_url=None,
)

app.include_router(health.router)
app.include_router(cluster.router)
app.include_router(resources.router)
app.include_router(query.router)
app.include_router(actions.router)


@app.middleware("http")
async def add_replica_header(request: Request, call_next):
    """Name the replica on every response. With two of them and an in-memory
    audit ring, knowing which one answered is the difference between a
    confusing /v1/audit result and an explicable one."""
    response = await call_next(request)
    response.headers["X-Hatch-Replica"] = settings.pod_name
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal error", "code": "internal"},
    )


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port, access_log=False)


if __name__ == "__main__":
    run()
