"""Grafana Cloud: PromQL, LogQL and alert state, through one Viewer token.

Two things here are the product of a documented mistake elsewhere in this repo:

* Datasources are matched by EXACT UID, never "the first one of type loki".
  The Cloud stack has three Loki datasources; two of them answer queries
  happily while containing none of this cluster's logs, so the wrong pick is a
  silent empty result rather than an error.
* UIDs are resolved from /api/frontend/settings, not /api/datasources. A Viewer
  token 403s on the latter — infra/grafana-dashboards/upgrade.sh documents this
  and exits on it. Do not "fix" that 403 by promoting the token to Admin.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from hatch_api.config import settings

logger = logging.getLogger(__name__)


class GrafanaUnavailable(RuntimeError):
    """Raised when a query cannot be served; becomes a 503 with the reason."""


_state: dict[str, Any] = {
    "checked": False,
    "prom_uid": None,
    "loki_uid": None,
    "available_uids": [],
    "error": None,
    "mode": settings.grafana_query_mode,
}


def state() -> dict[str, Any]:
    return dict(_state)


def _client() -> httpx.AsyncClient:
    if not settings.grafana_token:
        raise GrafanaUnavailable("no Grafana token configured")
    return httpx.AsyncClient(
        base_url=settings.grafana_url,
        headers={"Authorization": f"Bearer {settings.grafana_token}"},
        timeout=settings.grafana_timeout_seconds,
    )


async def verify() -> None:
    """Resolve and pin datasource UIDs once, at startup.

    A failure here degrades the query routes to 503 with a useful message; it
    never fails the pod, because losing history should not also lose /v1/pods.
    """
    _state["checked"] = True
    if not settings.grafana_enabled:
        _state["error"] = "grafana.enabled is false"
        return
    if not settings.grafana_token:
        _state["error"] = "grafana.enabled is true but no token is configured"
        logger.warning("grafana enabled without a token; query routes will 503")
        return

    uids: set[str] = set()
    try:
        async with _client() as c:
            resp = await c.get("/api/frontend/settings")
            if resp.status_code == 200:
                for ds in (resp.json().get("datasources") or {}).values():
                    if isinstance(ds, dict) and ds.get("uid"):
                        uids.add(ds["uid"])
            else:
                # Fall back for a token with more than Viewer. Not the primary
                # path precisely because Viewer cannot use it.
                resp = await c.get("/api/datasources")
                if resp.status_code == 200:
                    uids = {ds["uid"] for ds in resp.json() if ds.get("uid")}
                else:
                    raise GrafanaUnavailable(
                        f"could not list datasources: {resp.status_code}"
                    )
    except (httpx.HTTPError, GrafanaUnavailable) as exc:
        _state["error"] = f"datasource verification failed: {exc}"
        logger.warning("grafana datasource verification failed: %s", exc)
        return

    _state["available_uids"] = sorted(uids)
    if not settings.grafana_verify_datasources:
        _state["prom_uid"] = settings.grafana_prom_uid
        _state["loki_uid"] = settings.grafana_loki_uid
        return

    missing = []
    for label, want in (("prometheus", settings.grafana_prom_uid),
                        ("loki", settings.grafana_loki_uid)):
        if want in uids:
            _state[f"{'prom' if label == 'prometheus' else 'loki'}_uid"] = want
        else:
            missing.append(f"{label}={want!r}")
    if missing:
        _state["error"] = (
            f"configured datasource uid(s) not found: {', '.join(missing)}; "
            f"the stack has {sorted(uids)}"
        )
        logger.warning("%s", _state["error"])
    else:
        _state["error"] = None


def _require(kind: str) -> str:
    if not settings.grafana_enabled:
        raise GrafanaUnavailable("Grafana queries are disabled (grafana.enabled=false)")
    uid = _state.get(f"{kind}_uid")
    if not uid:
        raise GrafanaUnavailable(
            _state.get("error") or f"no verified {kind} datasource uid"
        )
    return uid


async def _proxy_get(uid: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    async with _client() as c:
        resp = await c.get(f"/api/datasources/proxy/uid/{uid}{path}", params=params)
        if resp.status_code >= 400:
            raise GrafanaUnavailable(
                f"datasource proxy returned {resp.status_code}: {resp.text[:400]}"
            )
        return resp.json()


async def prom_query(query: str, time: str | None = None) -> dict[str, Any]:
    uid = _require("prom")
    params: dict[str, Any] = {"query": query}
    if time:
        params["time"] = time
    return await _proxy_get(uid, "/api/v1/query", params)


async def prom_query_range(
    query: str, start: str, end: str, step: str,
) -> dict[str, Any]:
    uid = _require("prom")
    return await _proxy_get(
        uid, "/api/v1/query_range",
        {"query": query, "start": start, "end": end, "step": step},
    )


async def loki_query_range(
    query: str, start: str, end: str, limit: int, direction: str = "backward",
) -> dict[str, Any]:
    uid = _require("loki")
    return await _proxy_get(
        uid, "/loki/api/v1/query_range",
        {"query": query, "start": start, "end": end,
         "limit": limit, "direction": direction},
    )


async def loki_labels() -> dict[str, Any]:
    uid = _require("loki")
    return await _proxy_get(uid, "/loki/api/v1/labels", {})


async def alert_rules(states: set[str] | None) -> dict[str, Any]:
    """Grafana-managed alert rules and their current state.

    This is the half a scoped access-policy token could not give us, and the
    reason the credential is a Viewer service account rather than a
    logs:read/metrics:read policy token.
    """
    if not settings.grafana_enabled:
        raise GrafanaUnavailable("Grafana queries are disabled (grafana.enabled=false)")
    async with _client() as c:
        resp = await c.get("/api/prometheus/grafana/api/v1/rules")
        if resp.status_code >= 400:
            raise GrafanaUnavailable(
                f"alert rule read returned {resp.status_code}: {resp.text[:400]}"
            )
        doc = resp.json()

    out = []
    for group in (doc.get("data", {}).get("groups") or []):
        for rule in (group.get("rules") or []):
            rule_state = (rule.get("state") or "").lower()
            if states and rule_state not in states:
                continue
            out.append({
                "title": rule.get("name"),
                "folder": group.get("file"),
                "group": group.get("name"),
                "state": rule_state,
                "health": rule.get("health"),
                "lastEvaluation": rule.get("lastEvaluation"),
                "labels": rule.get("labels") or {},
                "annotations": rule.get("annotations") or {},
                "activeAt": [a.get("activeAt") for a in (rule.get("alerts") or [])][:5],
                "activeCount": len(rule.get("alerts") or []),
            })
    out.sort(key=lambda r: (r["state"] != "firing", r["title"] or ""))
    return {"count": len(out), "items": out}
