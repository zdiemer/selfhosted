"""The audit trail: a JSON line to stdout, plus an in-memory ring buffer.

Two sinks with different jobs. The stdout line is durable — infra/alloy ships
it to Loki, off-box, where it survives the pod. The ring buffer exists so
/v1/audit can answer immediately, including for an action performed a second
ago that Loki has not ingested yet.

The honest limitation, stated here and in the chart README: with two replicas
the ring buffer holds only what THIS replica did, so two consecutive /v1/audit
calls can return two different halves of the history and a restart loses one.
Loki is the source of truth. audit.source: loki merges the two and dedupes on
auditId, which is what that setting is for.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from hatch_api.config import settings

logger = logging.getLogger("hatch.audit")

_ring: deque[dict[str, Any]] = deque(maxlen=max(1, settings.audit_ring_size))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def record(event: str, actor: str, **fields: Any) -> dict[str, Any]:
    """Write one audit entry. Returns it, so a handler can echo the auditId."""
    entry: dict[str, Any] = {
        "event": event,
        "auditId": uuid.uuid4().hex,
        "ts": _now(),
        "actor": actor,
        "replica": settings.pod_name,
    }
    entry.update({k: v for k, v in fields.items() if v is not None})
    _ring.append(entry)
    logger.info("audit %s", event, extra={"hatch": entry})
    return entry


def recent(
    limit: int = 100,
    event: str | None = None,
    result: str | None = None,
) -> list[dict[str, Any]]:
    items = list(_ring)
    if event:
        items = [i for i in items if i.get("event") == event]
    if result:
        items = [i for i in items if i.get("result") == result]
    items.reverse()  # newest first
    return items[:limit]
