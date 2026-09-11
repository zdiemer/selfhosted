"""The mutations, their introspection endpoint, and the audit read."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

from hatch_api import audit, guards, k8s
from hatch_api.auth import require_act, require_read
from hatch_api.config import ApiKey, settings

router = APIRouter(prefix="/v1", tags=["actions"])


class RestartRequest(BaseModel):
    kind: str = Field(..., pattern="^(?i)(deployment|statefulset|daemonset)$")
    namespace: str
    name: str
    reason: str = Field(..., min_length=1, description="Recorded in the audit trail")


class DeletePodRequest(BaseModel):
    namespace: str
    name: str
    reason: str = Field(..., min_length=1)
    gracePeriodSeconds: int | None = Field(default=None, ge=0)
    force: bool = Field(default=False, description="Override the ownerless/RWO refusals")


class ScaleRequest(BaseModel):
    kind: str = Field(..., pattern="^(?i)(deployment|statefulset)$")
    namespace: str
    name: str
    replicas: int = Field(..., ge=0)
    reason: str = Field(..., min_length=1)


class CheckRequest(BaseModel):
    action: str = Field(..., pattern="^(restart|delete-pod|scale)$")
    kind: str = "Pod"
    namespace: str
    name: str


def _deny(caller: str, action: str, kind: str, namespace: str, name: str,
          decision: guards.Decision) -> HTTPException:
    audit.record("denied", caller, action=action, kind=kind, namespace=namespace,
                 name=name, code=decision.code, matchedRule=decision.matched_rule,
                 result="denied")
    # Name the workload AND the rule. An agent that gets a bare 403 retries.
    return HTTPException(status_code=403, detail={
        "detail": decision.reason, "code": decision.code, "action": action,
        "kind": kind, "namespace": namespace, "name": name,
        "matchedRule": decision.matched_rule,
    })


@router.get("/actions", summary="What hatch may do right now")
def introspect(_=Depends(require_read)) -> dict:
    """Lets the agent discover the rules instead of discovering them as a 403
    in the middle of an incident."""
    return {
        "enabled": settings.actions_enabled,
        "actions": {
            "restart": {
                "enabled": settings.actions_restart_enabled,
                "method": "POST", "path": "/v1/actions/restart",
                "body": {"kind": "Deployment|StatefulSet|DaemonSet",
                         "namespace": "str", "name": "str", "reason": "str"},
            },
            "delete-pod": {
                "enabled": settings.actions_delete_pod_enabled,
                "method": "POST", "path": "/v1/actions/delete-pod",
                "body": {"namespace": "str", "name": "str", "reason": "str",
                         "gracePeriodSeconds": "int?", "force": "bool?"},
            },
            "scale": {
                "enabled": settings.actions_scale_enabled,
                "method": "POST", "path": "/v1/actions/scale",
                "range": [settings.actions_scale_min, settings.actions_scale_max],
                "body": {"kind": "Deployment|StatefulSet", "namespace": "str",
                         "name": "str", "replicas": "int", "reason": "str"},
            },
        },
        "guards": {
            "denyWorkloads": settings.actions_deny_workloads,
            "denyNamespaces": sorted(settings.actions_deny_namespaces),
            "logsDenyNamespaces": sorted(settings.logs_deny_namespaces),
            "rbacActMode": settings.rbac_act_mode,
            "rbacActAllowNamespaces": sorted(settings.rbac_act_allow_namespaces),
            "cooldown": {"enabled": settings.actions_cooldown_enabled,
                         "seconds": settings.actions_cooldown_seconds},
            "notify": {"enabled": settings.actions_notify_enabled},
        },
        "dryRun": {"method": "POST", "path": "/v1/actions/check"},
    }


@router.post("/actions/check", summary="Dry-run the guard for an action")
def check(req: CheckRequest, _=Depends(require_read)) -> dict:
    """Read scope on purpose: asking "would this be allowed" must not require
    the act key, so a read-only agent can still plan and report."""
    return {
        "action": req.action, "kind": req.kind,
        "namespace": req.namespace, "name": req.name,
        **guards.check_action(req.action, req.kind, req.namespace, req.name).as_dict(),
    }


@router.post("/actions/restart", summary="Rollout restart a workload")
def restart(req: RestartRequest, caller: ApiKey = Depends(require_act)) -> dict:
    kind = req.kind.lower()
    decision = guards.check_action("restart", kind, req.namespace, req.name)
    if not decision.allowed:
        raise _deny(caller.name, "restart", kind, req.namespace, req.name, decision)

    if settings.actions_cooldown_enabled:
        # Read from the target's own annotation rather than local state, so the
        # cooldown survives hatch's own restarts and both replicas agree.
        last = k8s.last_restart_at(kind, req.namespace, req.name)
        if last:
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = None
            if age is not None and age < settings.actions_cooldown_seconds:
                raise _deny(caller.name, "restart", kind, req.namespace, req.name,
                            guards.Decision(
                                allowed=False, code="cooldown",
                                reason=(f"restarted {int(age)}s ago; cooldown is "
                                        f"{settings.actions_cooldown_seconds}s"),
                                matched_rule="actions.cooldown"))

    started = datetime.now(timezone.utc)
    try:
        result = k8s.rollout_restart(kind, req.namespace, req.name)
    except ApiException as exc:
        audit.record("action", caller.name, action="restart", kind=kind,
                     namespace=req.namespace, name=req.name, reason=req.reason,
                     result="error", error=str(exc.reason))
        raise HTTPException(exc.status or 502, detail=f"kubernetes API: {exc.reason}") from exc

    entry = audit.record(
        "action", caller.name, action="restart", kind=kind, namespace=req.namespace,
        name=req.name, reason=req.reason, result="ok",
        changed=result["changed"],
        durationMs=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
    )
    return {"ok": True, "auditId": entry["auditId"], **result}


@router.post("/actions/delete-pod", summary="Delete one pod")
def delete_pod(req: DeletePodRequest, caller: ApiKey = Depends(require_act)) -> dict:
    decision = guards.check_action("delete-pod", "pod", req.namespace, req.name)
    if not decision.allowed:
        raise _deny(caller.name, "delete-pod", "pod", req.namespace, req.name, decision)

    try:
        info = k8s.inspect_pod_for_delete(req.namespace, req.name)
    except ApiException as exc:
        raise HTTPException(exc.status or 502, detail=f"kubernetes API: {exc.reason}") from exc

    if not req.force:
        if not info["hasController"]:
            raise HTTPException(409, detail={
                "detail": ("this pod has no controller ownerReference: deleting it "
                           "deletes the service, because nothing will recreate it"),
                "code": "no_controller", "namespace": req.namespace, "name": req.name,
                "hint": "pass force:true only if you are certain",
            })
        # On democratic-csi iSCSI, RWO is single-node ATTACH. A singleton whose
        # replacement lands on another node blocks on FailedAttachVolume while
        # the original is already gone — the "fix" becomes the outage.
        if (info["rwoClaims"] and info["desiredReplicas"] == 1
                and info["strategy"] not in ("Recreate", None)):
            raise HTTPException(409, detail={
                "detail": (f"singleton on ReadWriteOnce volume(s) {info['rwoClaims']} with "
                           f"strategy {info['strategy']}: the replacement can land on "
                           "another node and block on FailedAttachVolume"),
                "code": "rwo_singleton", "namespace": req.namespace, "name": req.name,
                "hint": "restart the controller instead, or pass force:true",
            })

    try:
        k8s.delete_pod(req.namespace, req.name, req.gracePeriodSeconds)
    except ApiException as exc:
        audit.record("action", caller.name, action="delete-pod", namespace=req.namespace,
                     name=req.name, reason=req.reason, result="error", error=str(exc.reason))
        raise HTTPException(exc.status or 502, detail=f"kubernetes API: {exc.reason}") from exc

    entry = audit.record("action", caller.name, action="delete-pod",
                         namespace=req.namespace, name=req.name, reason=req.reason,
                         result="ok", controller=info["controllerName"])
    return {"ok": True, "auditId": entry["auditId"], "namespace": req.namespace,
            "name": req.name, "controllerKind": info["controllerKind"],
            "controllerName": info["controllerName"], "forced": req.force}


@router.post("/actions/scale", summary="Scale a Deployment or StatefulSet")
def scale(req: ScaleRequest, caller: ApiKey = Depends(require_act)) -> dict:
    kind = req.kind.lower()
    decision = guards.check_action("scale", kind, req.namespace, req.name)
    if not decision.allowed:
        raise _deny(caller.name, "scale", kind, req.namespace, req.name, decision)

    if not (settings.actions_scale_min <= req.replicas <= settings.actions_scale_max):
        raise HTTPException(400, detail={
            "detail": (f"replicas {req.replicas} outside the permitted range "
                       f"[{settings.actions_scale_min}, {settings.actions_scale_max}]"),
            "code": "scale_out_of_range",
        })

    try:
        result = k8s.scale(kind, req.namespace, req.name, req.replicas)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except ApiException as exc:
        audit.record("action", caller.name, action="scale", kind=kind,
                     namespace=req.namespace, name=req.name, reason=req.reason,
                     result="error", error=str(exc.reason))
        raise HTTPException(exc.status or 502, detail=f"kubernetes API: {exc.reason}") from exc

    entry = audit.record("action", caller.name, action="scale", kind=kind,
                         namespace=req.namespace, name=req.name, reason=req.reason,
                         result="ok", replicas=req.replicas)
    return {
        "ok": True, "auditId": entry["auditId"], **result,
        # The agent has no way to know this otherwise, and will be confused
        # when the count reverts hours later for no visible reason.
        "note": "helm reverts spec.replicas on the next upgrade of the owning chart",
    }


@router.get("/audit", summary="Recent actions, denials and log reads")
def read_audit(
    limit: int = Query(default=100, ge=1, le=1000),
    event: str | None = Query(default=None, description="action | denied | logs"),
    result: str | None = Query(default=None),
    _=Depends(require_read),
) -> dict:
    items = audit.recent(limit, event, result)
    return {
        "source": "memory",
        "replica": settings.pod_name,
        # Say it in the payload, not just the README: this replica's ring
        # buffer is not the whole history, and a caller reasoning about "did
        # anything else happen" needs to know where to look.
        "complete": False,
        "note": (
            "this replica's in-memory ring buffer only. The durable trail is in "
            'Loki: {namespace="' + settings.namespace + '", app="' + settings.release + '"} '
            "| json | event=~\"action|denied|logs\""
        ),
        "count": len(items),
        "items": items,
    }
