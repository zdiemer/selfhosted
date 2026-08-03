"""Digest formatting and the sms-relay call.

sms-relay (infra/sms-relay, a submodule) is a durable queue in front of an
Android handset. It answers 202 — "accepted", not "delivered" — and retries the
handset on its own schedule, so nothing here blocks on or retries the send.

Every message carries a date-scoped idempotency key. sms-relay dedupes on
(service, idempotency_key), so a job that is retried after a partial failure
cannot text you the same digest twice.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://sms-relay.infra.svc.cluster.local:8000"

# The handset splits anything long into segments. Keeping the whole digest under
# this is a deliberate ceiling on how many segments one run can cost.
MAX_BODY = 1200


class SmsRelay:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, dry_run: bool = False):
        self.base_url = (base_url or os.environ.get("APARTMENT_WATCH_SMS_URL") or DEFAULT_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("APARTMENT_WATCH_SMS_API_KEY", "")
        self.dry_run = dry_run

    def send(self, to: str, body: str, idempotency_key: str) -> bool:
        if self.dry_run:
            logger.info("[dry-run] would text %s (%d chars):\n%s", to, len(body), body)
            return True
        if not self.api_key:
            logger.error("APARTMENT_WATCH_SMS_API_KEY is unset — cannot send")
            return False

        payload = json.dumps(
            {"to": to, "body": body, "idempotency_key": idempotency_key}
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
                ids = [m.get("id") for m in data.get("messages", [])]
                logger.info("queued sms %s (HTTP %d)", ids, resp.status)
                return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            logger.error("sms-relay rejected the send: HTTP %d %s", exc.code, detail)
        except Exception as exc:
            logger.error("sms-relay unreachable: %s", exc)
        return False


def _money(n) -> str:
    try:
        return f"${int(n):,}".replace(",", "")
    except (TypeError, ValueError):
        return "$?"


def _beds(n) -> str:
    if n == 0:
        return "studio"
    if n is None:
        return "?br"
    return f"{int(n)}br"


def _parking_note(row) -> str:
    """The 'desired but not required' half of the parking rule, made visible.

    Parking never gates a match, so the only way it's useful is if the digest
    says which listings have it — and what it costs, when the listing said.
    """
    parking = row["parking"]
    if not parking or parking in ("none", "unknown", "street"):
        return ""
    label = {"garage": "garage", "carport": "carport", "off_street": "parking", "valet": "valet"}.get(parking, parking)
    fee = row["parking_fee"]
    if fee:
        return f" +{label} {_money(fee)}"
    return f" +{label}"


def format_digest(rows, total: int, max_listings: int, date_label: str, warnings: list[str]) -> str:
    """One SMS. Newest/cheapest first, capped, with an explicit overflow line.

    Truncation is stated rather than silent — "+4 more" tells you to go look,
    where a quietly cut list reads as the complete set of what matched.
    """
    shown = rows[:max_listings]
    header = f"apartment-watch {date_label} — {total} new"
    lines = [header]

    for row in shown:
        price = _money(row["effective_price"] or row["price"])
        if row["parking_fee"]:
            # Effective price folds in parking; show the base too so the number
            # in the text matches the number on the listing page.
            price = f"{price} ({_money(row['price'])}+{_money(row['parking_fee'])})"
        hood = row["neighborhood"] or "SF"
        lines.append(f"{price} {_beds(row['bedrooms'])} {hood}{_parking_note(row)} {row['url']}")

    if total > len(shown):
        lines.append(f"+{total - len(shown)} more")
    lines.extend(warnings)

    body = "\n".join(lines)
    if len(body) > MAX_BODY:
        # Drop listings from the end until it fits, keeping the header and the
        # overflow count honest.
        while len(shown) > 1 and len("\n".join(lines)) > MAX_BODY:
            shown = shown[:-1]
            lines = [header]
            for row in shown:
                price = _money(row["effective_price"] or row["price"])
                hood = row["neighborhood"] or "SF"
                lines.append(f"{price} {_beds(row['bedrooms'])} {hood}{_parking_note(row)} {row['url']}")
            lines.append(f"+{total - len(shown)} more")
            lines.extend(warnings)
        body = "\n".join(lines)[:MAX_BODY]
    return body


def format_health_alert(problems: list[str], date_label: str) -> str:
    """Sent when zero matches might mean 'broken' rather than 'quiet'."""
    return "\n".join(
        [f"apartment-watch {date_label} — no matches, but:"] + problems +
        ["Check `kubectl -n web logs job/...`"]
    )
