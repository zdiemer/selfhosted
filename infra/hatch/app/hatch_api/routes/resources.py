"""Live reads from the Kubernetes API.

Every handler here is `def`, not `async def`: the kubernetes client is
synchronous, so Starlette runs these in its threadpool. A blocking call in an
async route would stall the event loop and the readiness probe with it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from kubernetes.client.rest import ApiException

from hatch_api import audit, guards, k8s
from hatch_api.auth import require_read
from hatch_api.config import ApiKey, settings

router = APIRouter(prefix="/v1", tags=["resources"])


def _api_error(exc: ApiException) -> HTTPException:
    return HTTPException(
        status_code=exc.status if 400 <= (exc.status or 500) < 600 else 502,
        detail=f"kubernetes API: {exc.reason}",
    )


@router.get("/nodes", summary="Node readiness, pressure, taints, versions")
def nodes(_=Depends(require_read)) -> dict:
    try:
        items = k8s.list_nodes()
    except ApiException as exc:
        raise _api_error(exc) from exc
    return {"count": len(items), "items": items}


@router.get("/workloads", summary="Deployments, StatefulSets and DaemonSets")
def workloads(
    namespace: str | None = Query(default=None),
    kind: str | None = Query(default=None, pattern="^(?i)(deployment|statefulset|daemonset)$"),
    unhealthy: bool = Query(default=False, description="Only ready != desired"),
    name: str | None = Query(default=None, description="Glob, e.g. 'jelly*'"),
    limit: int = Query(default=200, ge=1, le=1000),
    _=Depends(require_read),
) -> dict:
    try:
        return k8s.list_workloads(namespace, kind, unhealthy, name, limit)
    except ApiException as exc:
        raise _api_error(exc) from exc


@router.get("/pods", summary="Pods, with a problems filter")
def pods(
    namespace: str | None = Query(default=None),
    labelSelector: str | None = Query(default=None),
    fieldSelector: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    problems: bool = Query(default=False, description="Not Running/Ready, or waiting on an error"),
    limit: int = Query(default=200, ge=1, le=1000),
    _=Depends(require_read),
) -> dict:
    try:
        return k8s.list_pods(namespace, labelSelector, fieldSelector, phase, problems, limit)
    except ApiException as exc:
        raise _api_error(exc) from exc


@router.get("/pods/{namespace}/{name}", summary="One pod, in detail")
def pod(namespace: str = Path(...), name: str = Path(...), _=Depends(require_read)) -> dict:
    try:
        return k8s.get_pod(namespace, name)
    except ApiException as exc:
        raise _api_error(exc) from exc


@router.get("/events", summary="Recent events, newest first")
def events(
    namespace: str | None = Query(default=None),
    type: str | None = Query(default=None, description="Normal or Warning"),
    sinceMinutes: int = Query(default=60, ge=1, le=1440),
    limit: int = Query(default=100, ge=1, le=1000),
    _=Depends(require_read),
) -> dict:
    try:
        return k8s.list_events(namespace, type, sinceMinutes, limit)
    except ApiException as exc:
        raise _api_error(exc) from exc


@router.get("/pods/{namespace}/{name}/logs", summary="Pod logs (deny-listed, redacted)")
def pod_logs(
    namespace: str = Path(...),
    name: str = Path(...),
    container: str | None = Query(default=None),
    tailLines: int | None = Query(default=None, ge=1),
    sinceSeconds: int | None = Query(default=None, ge=1),
    previous: bool = Query(default=False, description="The previous container instance"),
    timestamps: bool = Query(default=False),
    grep: str | None = Query(default=None, description="Keep only matching lines (regex)"),
    caller: ApiKey = Depends(require_read),
) -> dict:
    decision = guards.check_logs(namespace)
    if not decision.allowed:
        audit.record("denied", caller.name, action="logs", namespace=namespace,
                     name=name, code=decision.code, matchedRule=decision.matched_rule)
        raise HTTPException(status_code=403, detail={
            "detail": decision.reason, "code": decision.code,
            "namespace": namespace, "name": name,
            "matchedRule": decision.matched_rule,
        })

    tail = min(tailLines or settings.logs_default_tail_lines, settings.logs_max_tail_lines)
    try:
        raw = k8s.pod_logs(namespace, name, container, tail, sinceSeconds, previous, timestamps)
    except ApiException as exc:
        raise _api_error(exc) from exc

    lines = raw.splitlines()
    if grep:
        import re
        try:
            pat = re.compile(grep)
        except re.error as exc:
            raise HTTPException(400, detail=f"bad grep regex: {exc}") from exc
        lines = [ln for ln in lines if pat.search(ln)]

    redacted = 0
    if settings.logs_redact_enabled and settings.logs_redact_patterns:
        out = []
        for ln in lines:
            new = ln
            for pat in settings.logs_redact_patterns:
                new = pat.sub("[REDACTED]", new)
            redacted += new != ln
            out.append(new)
        lines = out

    # The sensitive read belongs in the same trail as the mutations.
    audit.record("logs", caller.name, namespace=namespace, name=name,
                 container=container, lineCount=len(lines), result="ok")

    return {
        "namespace": namespace, "pod": name, "container": container,
        "previous": previous, "lineCount": len(lines),
        "redactedLines": redacted, "lines": lines,
    }
