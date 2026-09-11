"""Kubernetes reads, projections, and the three mutations.

Every function here is SYNCHRONOUS, because the official client is. Routes that
call into this module are declared `def`, not `async def`, so Starlette runs
them in its threadpool — a blocking call inside an async route would stall the
event loop and, with it, the readiness probe.

The projections matter as much as the calls. A raw V1Pod is ~4KB of mostly
managedFields; the agent that reads this has a context budget, so everything
returned here is the smallest shape that still answers the question.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from hatch_api.config import settings

logger = logging.getLogger(__name__)

_core: client.CoreV1Api | None = None
_apps: client.AppsV1Api | None = None
_apps_patch: client.AppsV1Api | None = None

# Every patch this service sends is a strategic merge, and getting that wrong is
# silent rather than loud — see rollout_restart().
_STRATEGIC_MERGE = "application/strategic-merge-patch+json"


def init() -> None:
    global _core, _apps, _apps_patch
    try:
        config.load_incluster_config()
    except config.ConfigException:
        # Local development against a kubeconfig. In-cluster this never fires.
        config.load_kube_config()
    _core = client.CoreV1Api()
    _apps = client.AppsV1Api()

    # A SEPARATE client for patches, and the reason is a trap in the generated
    # code. Its patch methods negotiate the content type with
    # select_header_content_type([json-patch, merge-patch, strategic-merge,
    # apply-patch]), which returns content_types[0] whenever "application/json"
    # is not in the list — so every patch goes out as
    # application/json-patch+json, which expects a LIST of RFC-6902 ops, not the
    # dict body below. There is no per-call override: `_content_type` is not in
    # the method's all_params and is rejected outright.
    #
    # ApiClient.__call_api does `header_params.update(self.default_headers)`
    # AFTER the negotiated value is set, so a default header is the one thing
    # that reliably wins. It is scoped to its own client rather than the shared
    # one so it cannot leak onto an unrelated request.
    patch_client = client.ApiClient()
    patch_client.set_default_header("Content-Type", _STRATEGIC_MERGE)
    _apps_patch = client.AppsV1Api(patch_client)


def core() -> client.CoreV1Api:
    if _core is None:
        raise RuntimeError("kubernetes client not initialised")
    return _core


def apps() -> client.AppsV1Api:
    if _apps is None:
        raise RuntimeError("kubernetes client not initialised")
    return _apps


def apps_patch() -> client.AppsV1Api:
    """The AppsV1Api whose patches go out as strategic merge. See init()."""
    if _apps_patch is None:
        raise RuntimeError("kubernetes client not initialised")
    return _apps_patch


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _age(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _container_state(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    if state.running is not None:
        return {"state": "running", "startedAt": _iso(state.running.started_at)}
    if state.waiting is not None:
        return {"state": "waiting", "reason": state.waiting.reason,
                "message": state.waiting.message}
    if state.terminated is not None:
        t = state.terminated
        return {"state": "terminated", "reason": t.reason, "exitCode": t.exit_code,
                "finishedAt": _iso(t.finished_at)}
    return {}


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def list_nodes() -> list[dict[str, Any]]:
    out = []
    for n in core().list_node().items:
        conds = {c.type: c for c in (n.status.conditions or [])}
        ready = conds.get("Ready")
        pressure = [
            t for t in ("MemoryPressure", "DiskPressure", "PIDPressure")
            if conds.get(t) is not None and conds[t].status == "True"
        ]
        out.append({
            "name": n.metadata.name,
            "ready": ready is not None and ready.status == "True",
            "readyReason": ready.reason if ready is not None else None,
            "unschedulable": bool(n.spec.unschedulable),
            "pressure": pressure,
            "kubeletVersion": n.status.node_info.kubelet_version if n.status.node_info else None,
            "osImage": n.status.node_info.os_image if n.status.node_info else None,
            "taints": [
                {"key": t.key, "value": t.value, "effect": t.effect}
                for t in (n.spec.taints or [])
            ],
            "capacity": {k: v for k, v in (n.status.capacity or {}).items()
                         if k in ("cpu", "memory", "pods")},
            "allocatable": {k: v for k, v in (n.status.allocatable or {}).items()
                            if k in ("cpu", "memory", "pods")},
            "age": _age(n.metadata.creation_timestamp),
        })
    return sorted(out, key=lambda d: d["name"])


def project_pod(p: Any) -> dict[str, Any]:
    statuses = p.status.container_statuses or []
    ready_count = sum(1 for c in statuses if c.ready)
    restarts = sum(c.restart_count or 0 for c in statuses)
    owner = (p.metadata.owner_references or [None])[0]

    # The reason a pod is unhappy is usually in a container's waiting state,
    # not in pod.status.reason — surfacing it here saves a second round trip.
    reason = p.status.reason
    if not reason:
        for c in statuses:
            w = (c.state.waiting if c.state else None)
            if w is not None and w.reason not in (None, "ContainerCreating"):
                reason = w.reason
                break

    return {
        "namespace": p.metadata.namespace,
        "name": p.metadata.name,
        "node": p.spec.node_name,
        "phase": p.status.phase,
        "ready": f"{ready_count}/{len(statuses)}" if statuses else "0/0",
        "restarts": restarts,
        "reason": reason,
        "age": _age(p.metadata.creation_timestamp),
        "ownerKind": owner.kind if owner else None,
        "ownerName": owner.name if owner else None,
        "containers": [
            {
                "name": c.name,
                "image": c.image,
                "ready": c.ready,
                "restartCount": c.restart_count,
                **_container_state(c.state),
            }
            for c in statuses
        ],
    }


def _is_problem(pod: dict[str, Any]) -> bool:
    """Is this pod worth an operator's attention?

    Succeeded is checked FIRST and always wins. A completed Job pod reports
    ready 0/1 forever, so a naive ready-vs-desired test flags every backup that
    has ever run — this cluster has ~120 of them, which would bury the two pods
    that are actually broken.
    """
    phase = pod["phase"]
    if phase == "Succeeded":
        return False
    if phase != "Running":
        return True
    ready, _, total = pod["ready"].partition("/")
    if total and ready != total:
        return True
    return any(
        c.get("state") == "waiting" and c.get("reason") not in (None, "ContainerCreating")
        for c in pod["containers"]
    )


def list_pods(
    namespace: str | None = None,
    label_selector: str | None = None,
    field_selector: str | None = None,
    phase: str | None = None,
    problems: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector

    if namespace:
        raw = core().list_namespaced_pod(namespace, **kwargs).items
    else:
        raw = core().list_pod_for_all_namespaces(**kwargs).items

    pods = [project_pod(p) for p in raw]
    if phase:
        pods = [p for p in pods if (p["phase"] or "").lower() == phase.lower()]
    if problems:
        pods = [p for p in pods if _is_problem(p)]
    pods.sort(key=lambda d: (d["namespace"], d["name"]))
    return {"count": len(pods), "truncated": len(pods) > limit, "items": pods[:limit]}


def get_pod(namespace: str, name: str) -> dict[str, Any]:
    p = core().read_namespaced_pod(name, namespace)
    out = project_pod(p)
    out["conditions"] = [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in (p.status.conditions or [])
    ]
    out["volumes"] = [
        {"name": v.name,
         "claim": v.persistent_volume_claim.claim_name if v.persistent_volume_claim else None}
        for v in (p.spec.volumes or [])
    ]
    return out


_WORKLOAD_READERS = {
    "deployment": ("list_deployment_for_all_namespaces", "list_namespaced_deployment",
                   "read_namespaced_deployment"),
    "statefulset": ("list_stateful_set_for_all_namespaces", "list_namespaced_stateful_set",
                    "read_namespaced_stateful_set"),
    "daemonset": ("list_daemon_set_for_all_namespaces", "list_namespaced_daemon_set",
                  "read_namespaced_daemon_set"),
}


def _project_workload(kind: str, w: Any) -> dict[str, Any]:
    st, spec = w.status, w.spec
    if kind == "daemonset":
        desired = st.desired_number_scheduled or 0
        ready = st.number_ready or 0
        updated = st.updated_number_scheduled or 0
        available = st.number_available or 0
        strategy = spec.update_strategy.type if spec.update_strategy else None
    else:
        desired = spec.replicas if spec.replicas is not None else 0
        ready = st.ready_replicas or 0
        updated = st.updated_replicas or 0
        available = getattr(st, "available_replicas", None) or 0
        strategy = (
            spec.strategy.type if kind == "deployment" and spec.strategy
            else (spec.update_strategy.type if spec.update_strategy else None)
        )

    tmpl_ann = (w.spec.template.metadata.annotations or {}) if w.spec.template else {}
    return {
        "kind": kind,
        "namespace": w.metadata.namespace,
        "name": w.metadata.name,
        "desired": desired,
        "ready": ready,
        "updated": updated,
        "available": available,
        "healthy": desired == ready and desired > 0,
        "strategy": strategy,
        "images": [c.image for c in (w.spec.template.spec.containers or [])] if w.spec.template else [],
        "generation": w.metadata.generation,
        "observedGeneration": st.observed_generation,
        "restartedAt": tmpl_ann.get("kubectl.kubernetes.io/restartedAt"),
        "age": _age(w.metadata.creation_timestamp),
    }


def list_workloads(
    namespace: str | None = None,
    kind: str | None = None,
    unhealthy: bool = False,
    name_glob: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    kinds = [kind.lower()] if kind else list(_WORKLOAD_READERS)
    items: list[dict[str, Any]] = []
    for k in kinds:
        if k not in _WORKLOAD_READERS:
            continue
        all_fn, ns_fn, _ = _WORKLOAD_READERS[k]
        raw = (getattr(apps(), ns_fn)(namespace) if namespace
               else getattr(apps(), all_fn)()).items
        items.extend(_project_workload(k, w) for w in raw)

    if unhealthy:
        items = [i for i in items if not i["healthy"]]
    if name_glob:
        pat = re.compile(name_glob.replace("*", ".*"))
        items = [i for i in items if pat.fullmatch(i["name"])]
    items.sort(key=lambda d: (d["namespace"], d["kind"], d["name"]))
    return {"count": len(items), "truncated": len(items) > limit, "items": items[:limit]}


def get_workload(kind: str, namespace: str, name: str) -> Any:
    k = kind.lower()
    if k not in _WORKLOAD_READERS:
        raise ValueError(f"unsupported kind {kind!r}")
    return getattr(apps(), _WORKLOAD_READERS[k][2])(name, namespace)


def list_events(
    namespace: str | None = None,
    event_type: str | None = None,
    since_minutes: int = 60,
    limit: int = 100,
) -> dict[str, Any]:
    raw = (core().list_namespaced_event(namespace) if namespace
           else core().list_event_for_all_namespaces()).items
    cutoff = datetime.now(timezone.utc).timestamp() - since_minutes * 60

    out = []
    for e in raw:
        when = e.last_timestamp or e.event_time or e.first_timestamp
        if when is not None and when.timestamp() < cutoff:
            continue
        if event_type and (e.type or "").lower() != event_type.lower():
            continue
        obj = e.involved_object
        out.append({
            "lastTimestamp": _iso(when),
            "type": e.type,
            "reason": e.reason,
            "namespace": e.metadata.namespace,
            "object": f"{obj.kind}/{obj.name}" if obj else None,
            "message": e.message,
            "count": e.count,
        })
    out.sort(key=lambda d: d["lastTimestamp"] or "", reverse=True)
    return {"count": len(out), "truncated": len(out) > limit, "items": out[:limit]}


def pod_logs(
    namespace: str,
    name: str,
    container: str | None = None,
    tail_lines: int = 200,
    since_seconds: int | None = None,
    previous: bool = False,
    timestamps: bool = False,
) -> str:
    kwargs: dict[str, Any] = {
        "tail_lines": tail_lines,
        "previous": previous,
        "timestamps": timestamps,
        "limit_bytes": settings.logs_max_bytes,
    }
    if container:
        kwargs["container"] = container
    if since_seconds:
        kwargs["since_seconds"] = since_seconds
    return core().read_namespaced_pod_log(name, namespace, **kwargs)


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------

_PATCHERS = {
    "deployment": "patch_namespaced_deployment",
    "statefulset": "patch_namespaced_stateful_set",
    "daemonset": "patch_namespaced_daemon_set",
}


def rollout_restart(kind: str, namespace: str, name: str) -> dict[str, Any]:
    """The same patch `kubectl rollout restart` sends.

    Three details, each a silent bug if missed:

    1. The content type must be STRATEGIC merge, which is why this goes through
       apps_patch() rather than apps() — see init(). The client's default of
       application/json-patch+json rejects this dict body outright, and a plain
       application/merge-patch+json would silently REPLACE the whole annotations
       map, deleting the target chart's checksum/secret and friends and
       triggering an unrelated rollout.
    2. The value has to change. Writing the same restartedAt is a no-op that
       still returns 200, so compare metadata.generation before and after and
       report changed=false rather than a restart that did not happen.
    3. strategy=Recreate and updateStrategy=OnDelete both make this mean
       something other than "a rolling restart" — returned as warnings, since
       the caller is deciding whether it just made things worse.
    """
    k = kind.lower()
    if k not in _PATCHERS:
        raise ValueError(f"unsupported kind {kind!r}")

    before = get_workload(k, namespace, name)
    generation_before = before.metadata.generation

    warnings: list[str] = []
    spec = before.spec
    if k == "deployment" and spec.strategy and spec.strategy.type == "Recreate":
        warnings.append(
            "strategy is Recreate: every pod stops before a replacement starts, "
            "so this restart is a real outage for this workload"
        )
    if k in ("statefulset", "daemonset") and spec.update_strategy \
            and spec.update_strategy.type == "OnDelete":
        warnings.append(
            "updateStrategy is OnDelete: the annotation is patched but no pod will "
            "roll until it is deleted, so this restart will appear to do nothing"
        )

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    body = {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": stamp,
    }}}}}
    after = getattr(apps_patch(), _PATCHERS[k])(name, namespace, body)
    return {
        "kind": k,
        "namespace": namespace,
        "name": name,
        "restartedAt": stamp,
        "generationBefore": generation_before,
        "generationAfter": after.metadata.generation,
        "changed": after.metadata.generation != generation_before,
        "strategy": spec.strategy.type if (k == "deployment" and spec.strategy)
        else (spec.update_strategy.type if spec.update_strategy else None),
        "warnings": warnings,
    }


def last_restart_at(kind: str, namespace: str, name: str) -> str | None:
    """Read the target's own restartedAt — a cooldown that needs no state."""
    try:
        w = get_workload(kind, namespace, name)
    except (ApiException, ValueError):
        return None
    tmpl = w.spec.template if w.spec else None
    ann = (tmpl.metadata.annotations or {}) if tmpl else {}
    return ann.get("kubectl.kubernetes.io/restartedAt")


def inspect_pod_for_delete(namespace: str, name: str) -> dict[str, Any]:
    """Everything the delete guard needs, in one read.

    Two shapes are refused downstream:

    * No controller ownerReference — nothing will recreate it, so "delete the
      stuck pod" becomes "delete the service".
    * A singleton on an RWO volume whose controller is not Recreate. On
      democratic-csi iSCSI, RWO is single-node ATTACH: the replacement lands
      elsewhere and blocks on FailedAttachVolume while the original is gone.
    """
    p = core().read_namespaced_pod(name, namespace)
    owner = (p.metadata.owner_references or [None])[0]

    claims = [v.persistent_volume_claim.claim_name
              for v in (p.spec.volumes or []) if v.persistent_volume_claim]
    rwo_claims: list[str] = []
    for claim in claims:
        try:
            pvc = core().read_namespaced_persistent_volume_claim(claim, namespace)
        except ApiException:
            continue
        if "ReadWriteOnce" in (pvc.spec.access_modes or []):
            rwo_claims.append(claim)

    controller_kind = controller_name = None
    desired = None
    strategy = None
    if owner is not None:
        controller_kind, controller_name = owner.kind, owner.name
        # A pod's owner is a ReplicaSet; the Deployment above it is what
        # carries the strategy that decides whether a delete is survivable.
        if owner.kind == "ReplicaSet":
            try:
                rs = apps().read_namespaced_replica_set(owner.name, namespace)
                rs_owner = (rs.metadata.owner_references or [None])[0]
                if rs_owner is not None:
                    controller_kind, controller_name = rs_owner.kind, rs_owner.name
            except ApiException:
                pass
        try:
            if controller_kind in ("Deployment", "StatefulSet"):
                w = get_workload(controller_kind, namespace, controller_name)
                desired = w.spec.replicas
                strategy = (w.spec.strategy.type if controller_kind == "Deployment"
                            and w.spec.strategy else
                            (w.spec.update_strategy.type if w.spec.update_strategy else None))
        except (ApiException, ValueError):
            pass

    return {
        "hasController": owner is not None,
        "ownerKind": owner.kind if owner else None,
        "ownerName": owner.name if owner else None,
        "controllerKind": controller_kind,
        "controllerName": controller_name,
        "desiredReplicas": desired,
        "strategy": strategy,
        "rwoClaims": rwo_claims,
    }


def delete_pod(namespace: str, name: str, grace_period_seconds: int | None = None) -> None:
    kwargs: dict[str, Any] = {}
    if grace_period_seconds is not None:
        kwargs["grace_period_seconds"] = grace_period_seconds
    core().delete_namespaced_pod(name, namespace, **kwargs)


_SCALE_READERS = {
    "deployment": ("read_namespaced_deployment_scale", "patch_namespaced_deployment_scale"),
    "statefulset": ("read_namespaced_stateful_set_scale", "patch_namespaced_stateful_set_scale"),
}


def scale(kind: str, namespace: str, name: str, replicas: int) -> dict[str, Any]:
    """Via the /scale subresource only — never a patch of the parent object,
    which would also permit rewriting its image, env and serviceAccountName."""
    k = kind.lower()
    if k not in _SCALE_READERS:
        raise ValueError(
            f"cannot scale {kind}: only Deployment and StatefulSet have a /scale "
            "subresource (a DaemonSet's replica count is the node count)"
        )
    read_fn, patch_fn = _SCALE_READERS[k]
    current = getattr(apps(), read_fn)(name, namespace)
    before = current.spec.replicas
    getattr(apps_patch(), patch_fn)(name, namespace, {"spec": {"replicas": replicas}})
    return {"kind": k, "namespace": namespace, "name": name,
            "from": before, "to": replicas}


def api_reachable() -> tuple[bool, float, str | None]:
    """Cheap liveness check against the API server, for /v1/ready."""
    started = datetime.now(timezone.utc)
    try:
        core().get_api_resources()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, 0.0, str(exc)
    ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    return True, round(ms, 1), None
