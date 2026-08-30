"""The sms-relay call.

sms-relay (infra/sms-relay) is a durable queue in front of an Android handset.
It answers 202 — "accepted", not "delivered" — and does its own retrying, so
nothing here blocks or retries. A failed send is recorded against the cycle and
the laundry keeps being monitored.

THE IDEMPOTENCY KEY IS SCOPED BY CYCLE, and that is not a detail. sms-relay
dedupes on (service, idempotency_key): a key of "laundry-washer" would send the
first text ever and then answer 202 to every load after it, returning the
ORIGINAL message id, having sent nothing. The caller sees success. You see no
texts. life/carson's notify.py carries the same warning, learned the same way in
web/apartment-watch — so here the key carries the cycle's start timestamp, which
is unique per load by construction.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from detect import MachineConfig, format_duration

logger = logging.getLogger("laundry.notify")

# Two SMS segments. Android caps how many messages an app may send in a rolling
# window and EACH CONCATENATED SEGMENT COUNTS toward it, so a long body is
# several messages against that budget.
MAX_BODY = 300


def _zone() -> ZoneInfo:
    """The pod's TZ. Set from `timeZone` in values.yaml; UTC is a poor default
    for a text that says "finished 3:42 PM", so a missing/invalid zone is loud."""
    name = os.environ.get("TZ", "UTC")
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — bad tzdata must not stop the text
        logger.warning("unknown TZ %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def compose(cfg: MachineConfig, started_at: float, ended_at: float) -> str:
    """Render the machine's message template.

    Finish time is the moment the machine actually went still, not the moment
    the quiet timer expired — so the text reads "finished 3:42 PM" even though
    it arrives at 3:47. That is the honest number, and the one you compare
    against the appliance's own display when checking whether this thing works.
    """
    local = datetime.fromtimestamp(ended_at, _zone())
    body = cfg.message.format(
        label=cfg.label,
        duration=format_duration(ended_at - started_at),
        finished=local.strftime("%-I:%M %p"),
        device=cfg.id,
    )
    return body[:MAX_BODY]


def idempotency_key(device: str, started_at: float) -> str:
    """Unique per load. See the module docstring for what a coarser key costs."""
    return f"laundry-{device}-{int(started_at)}"


def send(cfg: MachineConfig, started_at: float, ended_at: float) -> tuple[bool, str | None]:
    """Text that a load is done. Returns (ok, error) — never raises.

    A send that blew up must not take down the sample ingest with it: the dryer
    still needs watching even when the handset is off wifi.
    """
    body = compose(cfg, started_at, ended_at)
    key = idempotency_key(cfg.id, started_at)

    if config.DRY_RUN:
        logger.info("[dry-run] would text %s: %s (key=%s)", config.SMS_TO, body, key)
        return True, None
    if not config.SMS_API_KEY:
        return False, "LAUNDRY_SMS_API_KEY is unset"
    if not config.SMS_TO:
        return False, "LAUNDRY_SMS_TO is unset"

    payload = json.dumps(
        {"to": config.SMS_TO, "body": body, "idempotency_key": key}
    ).encode()
    req = urllib.request.Request(
        f"{config.SMS_URL.rstrip('/')}/api/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Key": config.SMS_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read() or b"{}")
            for m in data.get("messages", []):
                # created_at is logged because an idempotent replay is also a
                # 202 with a message id and is otherwise indistinguishable from
                # a fresh send — an old timestamp here means nothing was sent.
                logger.info(
                    "sms-relay accepted id=%s status=%s created_at=%s key=%s",
                    m.get("id"), m.get("status"), m.get("created_at"), key,
                )
            return True, None
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        logger.error("sms-relay %s: %s", exc.code, detail)
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.error("sms-relay unreachable: %s", exc)
        return False, str(exc)
