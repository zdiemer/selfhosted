"""One JSON object per log record, on stdout.

infra/alloy ships this stream to Loki because the pod carries
logging.zachd/external-ingress. For hatch that stream is the audit record, not
debug output, so the format is a contract rather than a preference.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # audit.py attaches its already-shaped dict here; it wins over the
        # message so an audit line is queryable by field in Loki.
        extra = getattr(record, "hatch", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # uvicorn's access log would duplicate Traefik's, at a lower fidelity, and
    # would put query strings (which carry PromQL, not credentials, but still)
    # into a second store. Traefik's access log is the request record.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpcore", "kubernetes.client.rest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
