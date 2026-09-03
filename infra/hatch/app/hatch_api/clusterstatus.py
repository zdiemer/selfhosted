"""Cached passthrough of infra/cluster-status's status.json.

status.json is ~165KB, which is roughly 45k tokens. Handing that to an agent
whole would consume its entire context on the first call, so the projection
here is not an optimisation — it is what makes the endpoint usable.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from hatch_api.config import settings

logger = logging.getLogger(__name__)

_cache: tuple[float, dict[str, Any]] | None = None


def fetch(force: bool = False) -> dict[str, Any]:
    """Return status.json, cached for cacheSeconds.

    The cache floor is the collector's own interval: below it, hatch's two
    replicas poll two different collectors (that Service is sessionAffinity:
    ClientIP) and answer with different generatedAt values for no benefit.
    """
    global _cache
    now = time.monotonic()
    if not force and _cache is not None:
        age, doc = _cache
        if now - age < settings.cluster_status_cache_seconds:
            return doc

    with httpx.Client(timeout=settings.cluster_status_timeout_seconds) as c:
        resp = c.get(settings.cluster_status_url)
        resp.raise_for_status()
        doc = resp.json()
    _cache = (now, doc)
    return doc


def index(doc: dict[str, Any]) -> dict[str, Any]:
    """Key names, item counts and byte sizes — so the agent can decide what to
    ask for before it pays for it."""
    out = {}
    for key, value in doc.items():
        out[key] = {
            "bytes": len(json.dumps(value, default=str)),
            "items": len(value) if isinstance(value, (list, dict)) else None,
            "type": type(value).__name__,
        }
    return out


def project(doc: dict[str, Any], sections: list[str] | None) -> dict[str, Any]:
    if sections == ["all"]:
        return dict(doc)
    wanted = sections or settings.cluster_status_default_sections
    return {k: v for k, v in doc.items() if k in wanted}


def summarise(doc: dict[str, Any]) -> dict[str, Any]:
    """The compact digest behind /v1/cluster/summary."""
    nodes = doc.get("nodeDisks") or doc.get("nodes") or []
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    ready = sum(1 for n in nodes if isinstance(n, dict) and n.get("ready", True))

    problems = doc.get("problemPods") or []
    unhealthy: list[dict[str, Any]] = []
    for key, kind in (("deployments", "Deployment"),
                      ("statefulSets", "StatefulSet"),
                      ("daemonSets", "DaemonSet")):
        for w in doc.get(key) or []:
            if not isinstance(w, dict):
                continue
            desired = w.get("desired", w.get("replicas"))
            avail = w.get("ready", w.get("available"))
            if desired is not None and avail is not None and desired != avail:
                unhealthy.append({"kind": kind, "namespace": w.get("namespace"),
                                  "name": w.get("name"), "ready": avail, "desired": desired})

    return {
        "generatedAt": doc.get("generatedAt"),
        "nodes": {"ready": ready, "total": len(nodes)},
        "problemPods": {"count": len(problems), "items": problems[:20]},
        "unhealthyWorkloads": {"count": len(unhealthy), "items": unhealthy[:20]},
        "jobFailures": doc.get("jobFailures") or [],
    }
