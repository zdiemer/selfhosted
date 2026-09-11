"""Health. /healthz is shallow on purpose; /v1/ready is the deep one."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hatch_api import grafana, k8s
from hatch_api.auth import require_read
from hatch_api.config import settings

router = APIRouter()


@router.get("/healthz", tags=["health"], summary="Liveness and readiness")
async def healthz() -> dict[str, object]:
    """Touches nothing external, deliberately.

    A deep check here would let a slow API server make the kubelet kill the pod
    whose whole job is to tell you the API server is slow.
    """
    return {"status": "ok", "version": "v1", "replica": settings.pod_name}


@router.get("/v1/ready", tags=["health"], summary="Deep dependency check")
def ready(_=Depends(require_read)) -> dict[str, object]:
    ok, latency_ms, err = k8s.api_reachable()
    out: dict[str, object] = {
        "replica": settings.pod_name,
        "kubeApi": {"ok": ok, "latencyMs": latency_ms, "error": err},
        "grafana": grafana.state(),
    }
    if settings.cluster_status_enabled:
        from hatch_api import clusterstatus
        try:
            doc = clusterstatus.fetch()
            out["clusterStatus"] = {"ok": True, "generatedAt": doc.get("generatedAt")}
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            out["clusterStatus"] = {"ok": False, "error": str(exc)}
    else:
        out["clusterStatus"] = {"ok": False, "error": "disabled"}
    return out
