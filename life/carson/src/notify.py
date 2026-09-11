"""The sms-relay call, in Mr. Carson's voice.

sms-relay (infra/sms-relay) is a durable queue in front of an Android handset.
It answers 202 — "accepted", not "delivered" — and retries the handset itself,
so nothing here blocks on or retries a send.

TWO THINGS HERE ARE NOT STYLE CHOICES, both learned the hard way in
web/apartment-watch:

1. The idempotency key is scoped by date AND CONTENT. sms-relay dedupes on
   (service, idempotency_key). With a date-only key the first send of the day
   wins and every later one comes back 202 with the original message id, having
   sent NOTHING — the caller sees success and marks the work done. Hashing the
   body means a genuine retry still dedupes while different content goes out.

2. Bodies stay near MAX_BODY. Android caps how many SMS an app may send in a
   rolling window and EACH CONCATENATED SEGMENT COUNTS, so a 520-char body is
   four of them. That cap has silently dropped a message before.

The voice matters more than it looks: this is the only channel carson speaks
on, and "Sir, Rachel's birthday is Thursday and you have not yet acquired a
card" gets read where a notification gets swiped away.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("carson.notify")

DEFAULT_URL = "http://sms-relay.infra.svc.cluster.local:8000"

# ~2 SMS segments. See the note above about the Android rolling-window cap.
MAX_BODY = 460


def idempotency_key(scope: str, day: str, body: str) -> str:
    """Stable per (what, which day, exactly what it says).

    `scope` names the sender ("birthday-t7", "digest"); `day` is a date string.
    The body hash is the half that stops a same-day resend from being swallowed.
    """
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"carson-{scope}-{day}-{digest}"


def truncate(body: str, limit: int = MAX_BODY) -> str:
    """Trim to `limit`, on a word boundary, with an ellipsis that fits."""
    if len(body) <= limit:
        return body
    cut = body[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


class SmsRelay:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        to: str | None = None,
        dry_run: bool = False,
    ):
        self.base_url = (base_url or os.environ.get("CARSON_SMS_URL") or DEFAULT_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("CARSON_SMS_API_KEY", "")
        self.to = to or os.environ.get("CARSON_SMS_TO", "")
        self.dry_run = dry_run or os.environ.get("CARSON_DRY_RUN") == "1"

    def send(self, body: str, *, scope: str, day: str, to: str | None = None) -> bool:
        recipient = to or self.to
        body = truncate(body)
        key = idempotency_key(scope, day, body)

        if self.dry_run:
            logger.info("[dry-run] would text %s (%d chars, key=%s):\n%s",
                        recipient, len(body), key, body)
            return True
        if not self.api_key:
            logger.error("CARSON_SMS_API_KEY is unset — cannot send")
            return False
        if not recipient:
            logger.error("no recipient (CARSON_SMS_TO unset) — cannot send")
            return False

        payload = json.dumps(
            {"to": recipient, "body": body, "idempotency_key": key}
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/messages",
            data=payload,
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read() or b"{}")
                for m in data.get("messages", []):
                    # Log status AND created_at: an idempotent replay is a 202
                    # with a pre-existing message and is otherwise
                    # indistinguishable from a fresh send.
                    logger.info(
                        "sms-relay accepted id=%s status=%s created_at=%s key=%s",
                        m.get("id"), m.get("status"), m.get("created_at"), key,
                    )
                return True
        except urllib.error.HTTPError as exc:
            logger.error("sms-relay %s: %s", exc.code, exc.read()[:400].decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 — a failed text must never crash the run
            logger.error("sms-relay unreachable: %s", exc)
        return False
