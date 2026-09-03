"""cluster-status passthrough and the compact summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from hatch_api import clusterstatus
from hatch_api.auth import require_read
from hatch_api.config import settings

router = APIRouter(prefix="/v1/cluster", tags=["cluster"])


def _doc() -> dict:
    if not settings.cluster_status_enabled:
        raise HTTPException(503, detail="cluster-status passthrough is disabled")
    try:
        return clusterstatus.fetch()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502, detail=f"could not reach cluster-status: {exc}"
        ) from exc


@router.get("/summary", summary="Compact cluster health digest")
def summary(_=Depends(require_read)) -> dict:
    """The intended first call. Under a few KB, unlike /cluster/status."""
    return clusterstatus.summarise(_doc())


@router.get("/status", summary="cluster-status status.json, projected")
def status(
    section: str | None = Query(
        default=None,
        description=(
            "Comma-separated sections, or 'all', or 'index'. Defaults to a "
            "compact subset: the full document is ~165KB (~45k tokens)."
        ),
    ),
    _=Depends(require_read),
) -> dict:
    doc = _doc()
    if section == "index":
        return {"_source": "cluster-status", "generatedAt": doc.get("generatedAt"),
                "index": clusterstatus.index(doc)}
    sections = [s.strip() for s in section.split(",")] if section else None
    return {
        "_source": "cluster-status",
        "generatedAt": doc.get("generatedAt"),
        "sections": sections or settings.cluster_status_default_sections,
        "data": clusterstatus.project(doc, sections),
    }
