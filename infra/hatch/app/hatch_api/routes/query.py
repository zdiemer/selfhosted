"""PromQL, LogQL and alert state, proxied through Grafana Cloud."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from hatch_api import grafana
from hatch_api.auth import require_read
from hatch_api.config import settings

router = APIRouter(prefix="/v1", tags=["query"])


def _unavailable(exc: grafana.GrafanaUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "detail": str(exc),
        "code": "grafana_unavailable",
        "grafana": grafana.state(),
    })


def _window(since: str | None, start: str | None, end: str | None) -> tuple[str, str]:
    """Resolve a time window, bounded by grafana.maxLookbackHours.

    `since=6h` is the shape an agent reaches for; explicit start/end stays
    available for following up on something it already found.
    """
    if start and end:
        return start, end
    hours = 1.0
    if since:
        unit, value = since[-1], since[:-1]
        try:
            n = float(value)
        except ValueError as exc:
            raise HTTPException(400, detail=f"bad since {since!r}; use e.g. 30m, 6h, 2d") from exc
        hours = {"m": n / 60, "h": n, "d": n * 24}.get(unit, n)
    hours = min(hours, settings.grafana_max_lookback_hours)
    now = datetime.now(timezone.utc)
    return (
        (now - timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        now.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


@router.get("/metrics/query", summary="PromQL instant query")
async def metrics_query(
    query: str = Query(..., description="PromQL"),
    time: str | None = Query(default=None, description="RFC3339; default now"),
    _=Depends(require_read),
) -> dict:
    try:
        return await grafana.prom_query(query, time)
    except grafana.GrafanaUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/metrics/query_range", summary="PromQL range query")
async def metrics_query_range(
    query: str = Query(..., description="PromQL"),
    since: str | None = Query(default="1h", description="e.g. 30m, 6h, 2d"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    step: str | None = Query(default=None, description="Default: fits maxRangePoints"),
    _=Depends(require_read),
) -> dict:
    s, e = _window(since, start, end)
    if not step:
        # Bounded so a badly-formed agent query cannot blow the free-tier
        # budget: pick the step that fits the window into maxRangePoints.
        span = (datetime.fromisoformat(e.replace("Z", "+00:00"))
                - datetime.fromisoformat(s.replace("Z", "+00:00"))).total_seconds()
        step = f"{max(15, int(span // max(1, settings.grafana_max_range_points)))}s"
    try:
        return await grafana.prom_query_range(query, s, e, step)
    except grafana.GrafanaUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/logs/query", summary="LogQL range query")
async def logs_query(
    query: str = Query(..., description='LogQL, e.g. {namespace="media"} |= "error"'),
    since: str | None = Query(default="1h"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    direction: str = Query(default="backward", pattern="^(backward|forward)$"),
    _=Depends(require_read),
) -> dict:
    s, e = _window(since, start, end)
    n = min(limit or settings.grafana_logs_default_limit, settings.grafana_logs_max_limit)
    try:
        return await grafana.loki_query_range(query, s, e, n, direction)
    except grafana.GrafanaUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/logs/labels", summary="Loki label names")
async def logs_labels(_=Depends(require_read)) -> dict:
    """Cheap discovery, so the agent stops guessing stream selectors."""
    try:
        return await grafana.loki_labels()
    except grafana.GrafanaUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/alerts", summary="Grafana alert rules and their state")
async def alerts(
    state: str = Query(default="firing,pending", description="csv, or 'all'"),
    _=Depends(require_read),
) -> dict:
    states = None if state == "all" else {s.strip().lower() for s in state.split(",")}
    try:
        return await grafana.alert_rules(states)
    except grafana.GrafanaUnavailable as exc:
        raise _unavailable(exc) from exc
