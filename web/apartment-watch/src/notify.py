"""Digest formatting and the sms-relay call.

sms-relay (infra/sms-relay, a submodule) is a durable queue in front of an
Android handset. It answers 202 — "accepted", not "delivered" — and retries the
handset on its own schedule, so nothing here blocks on or retries the send.

Every message carries an idempotency key. sms-relay dedupes on
(service, idempotency_key), so a job retried after a partial failure cannot text
you the same digest twice.

That key is scoped by **date *and* content**, and the content half is not
optional. With a date-only key, the first send of the day wins and every later
one comes back as an "idempotent replay" — a 202 with the original message id,
having sent nothing. The caller sees success, marks its listings notified, and
they are never mentioned again. Hashing the listing set means a genuine retry
still dedupes while a different set of listings actually goes out.
"""

from __future__ import annotations

import hashlib
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
                msgs = data.get("messages", [])
                # Log status and created_at too: a replay comes back 202 with a
                # pre-existing message, which is indistinguishable from a fresh
                # send unless you look.
                for m in msgs:
                    logger.info(
                        "queued sms %s status=%s created=%s (HTTP %d)",
                        m.get("id"), m.get("status"), m.get("created_at"), resp.status,
                    )
                return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            logger.error("sms-relay rejected the send: HTTP %d %s", exc.code, detail)
        except Exception as exc:
            logger.error("sms-relay unreachable: %s", exc)
        return False


def digest_key(prefix: str, day: str, rows) -> str:
    """`apartment-watch:<day>:<hash of the listings>`.

    Same listings on a retry -> same key -> sms-relay dedupes. A different set
    -> a different key -> it actually sends.
    """
    ids = sorted(f"{r['source']}/{r['external_id']}" for r in rows)
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:12]
    return f"{prefix}:{day}:{digest}"


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


def _listing_line(row) -> str:
    price = _money(row["effective_price"] or row["price"])
    if row["parking_fee"]:
        # Effective price folds in parking; show the base too so the number in
        # the text matches the number on the listing page.
        price = f"{price} ({_money(row['price'])}+{_money(row['parking_fee'])})"
    hood = row["neighborhood"] or "SF"
    return f"{price} {_beds(row['bedrooms'])} {hood}{_parking_note(row)} {row['url']}"


def build_digest(rows, max_listings: int, max_messages: int, date_label: str, warnings: list[str]):
    """Return [(body, rows_in_that_body), ...] — one entry per SMS to send.

    Two rules make this correct rather than merely tidy:

    * **Only listings whose URL is actually in a body may be marked notified.**
      The first version put "+35 more" at the bottom and then retired all 40,
      so 35 listings you were never shown were never mentioned again. Returning
      the rows alongside each body is what lets the caller mark exactly what it
      sent.
    * **Overflow rolls forward.** Anything past the cap stays un-notified and
      leads the next run's digest, so "+N more" is a promise rather than a
      dead end — there is no UI to go and look at.

    Spilling into a few messages rather than one keeps a busy day readable
    without turning the first run, where every listing on the market is new,
    into a hundred-segment wall of text.
    """
    total = len(rows)
    chunks: list[tuple[str, list]] = []
    remaining = list(rows)

    while remaining and len(chunks) < max_messages:
        batch, body = [], ""
        part = len(chunks) + 1
        for row in remaining[:max_listings]:
            trial = batch + [row]
            head = f"apartment-watch {date_label}"
            if max_messages > 1 and (total > max_listings):
                head += f" ({part})"
            head += f" — {total} new"
            candidate = "\n".join([head] + [_listing_line(r) for r in trial])
            if len(candidate) > MAX_BODY and batch:
                break
            batch, body = trial, candidate
        if not batch:
            break
        remaining = remaining[len(batch):]
        chunks.append((body, batch))

    if not chunks:
        return []

    # Trailer goes on the last message only.
    left = len(remaining)
    body, batch = chunks[-1]
    tail = []
    if left:
        tail.append(f"+{left} more, in the next run")
    tail.extend(warnings)
    if tail:
        body = "\n".join([body] + tail)[:MAX_BODY]
        chunks[-1] = (body, batch)
    return chunks


def format_health_alert(problems: list[str], date_label: str) -> str:
    """Sent when zero matches might mean 'broken' rather than 'quiet'."""
    return "\n".join(
        [f"apartment-watch {date_label} — no matches, but:"] + problems +
        ["Check `kubectl -n web logs job/...`"]
    )
