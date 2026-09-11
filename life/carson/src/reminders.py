"""The birthday/anniversary ladder: T-21, T-7, T-1, day-of.

Deliberately dumb and deterministic. Deciding *that* a reminder is due is
arithmetic on a date, and arithmetic is not a job for a language model — the
Claude tier's contribution (phase 5) is the gift research that gets stapled
into the T-21 and T-7 bodies, nothing more. If Ollama and Claude are both down,
this still texts on time.

Idempotency is `reminder_log`'s UNIQUE (important_date_id, year, stage), taken
BEFORE the send and rolled back if the send fails. Claiming first means a
CronJob that runs twice concurrently cannot double-text; rolling back on
failure means a relay outage doesn't silently swallow the reminder forever.

Note what is NOT here: catch-up. If the pod was down for the T-7 window, T-7 is
gone — the T-1 will still fire. Texting "this was a week away, a week ago" is
worse than silence.
"""

from __future__ import annotations

import logging
from datetime import date

from db import now

logger = logging.getLogger("carson.reminders")

# days-before -> stage name.
LADDER = {21: "t21", 7: "t7", 1: "t1", 0: "t0"}


def next_occurrence(month: int, day: int, today: date) -> date:
    """The next time this month/day comes round, today included.

    Feb 29 is observed on Mar 1 in common years. The alternative (Feb 28) means
    the reminder for a leap-day birthday arrives the day before everyone else
    wishes them happy birthday.
    """
    def _on(year: int) -> date:
        try:
            return date(year, month, day)
        except ValueError:
            return date(year, 3, 1)  # Feb 29 in a common year

    candidate = _on(today.year)
    return candidate if candidate >= today else _on(today.year + 1)


def stage_for(days_until: int) -> str | None:
    return LADDER.get(days_until)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def compose(
    *,
    stage: str,
    name: str,
    kind: str,
    label: str,
    when: date,
    age: int | None,
    ideas: list[dict],
    past_gifts: list[str],
) -> str:
    """The message body, in Mr. Carson's register.

    Reminders land better as "Sir, Rachel's birthday is Thursday and you have
    not yet acquired a card" than as a notification, which is the entire reason
    this tool texts instead of pushing.
    """
    occasion = label or ("birthday" if kind == "birthday" else
                         "anniversary" if kind == "anniversary" else kind)
    possessive = f"{name}'s {occasion}"
    day_str = when.strftime("%a %-d %b")

    if stage == "t21":
        head = f"Sir — {possessive} falls on {day_str}, three weeks hence."
        if age is not None:
            head += f" The {_ordinal(age)}."
        if ideas:
            lines = "; ".join(
                f"{i+1}) {d['title']}" + (f" ({d['price']})" if d.get("price") else "")
                for i, d in enumerate(ideas[:4])
            )
            return f"{head} Some suggestions: {lines}. Full list on the dashboard."
        avoid = f" Previously given: {', '.join(past_gifts[:3])}." if past_gifts else ""
        return f"{head} I shall look into a suitable gift.{avoid}"

    if stage == "t7":
        head = f"Sir — {possessive} is a week away ({day_str})."
        tail = " I would order by Wednesday if it is to arrive in time."
        if ideas:
            lines = "; ".join(f"{i+1}) {d['title']}" for i, d in enumerate(ideas[:3]))
            return f"{head} The shortlist: {lines}.{tail}"
        return f"{head}{tail} Nothing has been chosen yet."

    if stage == "t1":
        return (f"Sir — {possessive} is tomorrow. "
                f"A card, at the very least, would not go amiss.")

    # t0
    head = f"Sir — it is {possessive} today."
    if age is not None:
        head += f" The {_ordinal(age)}."
    return f"{head} Do reach out."


def due_today(conn, today: date) -> list[dict]:
    """Every (date, stage) pair that should go out today and hasn't."""
    out: list[dict] = []
    rows = conn.execute(
        """
        SELECT d.id, d.kind, d.label, d.month, d.day, d.year, p.name
        FROM important_date d JOIN person p ON p.id = d.person_id
        """
    ).fetchall()

    for row in rows:
        when = next_occurrence(row["month"], row["day"], today)
        stage = stage_for((when - today).days)
        if stage is None:
            continue
        already = conn.execute(
            "SELECT 1 FROM reminder_log WHERE important_date_id=? AND year=? AND stage=?",
            (row["id"], when.year, stage),
        ).fetchone()
        if already:
            continue

        age = when.year - row["year"] if row["year"] else None

        ideas = [
            dict(r) for r in conn.execute(
                "SELECT title, url, price FROM gift_idea "
                "WHERE important_date_id=? AND year=? ORDER BY id",
                (row["id"], when.year),
            )
        ]
        past = [
            r["description"] for r in conn.execute(
                "SELECT description FROM gift_history WHERE important_date_id=? "
                "ORDER BY year DESC LIMIT 5",
                (row["id"],),
            )
        ]

        out.append({
            "date_id": row["id"], "name": row["name"], "kind": row["kind"],
            "label": row["label"], "when": when, "stage": stage, "age": age,
            "ideas": ideas, "past_gifts": past,
        })
    return out


def run(conn, sms, today: date | None = None) -> int:
    """Send everything due. Returns how many went out."""
    today = today or date.today()
    sent = 0
    for item in due_today(conn, today):
        body = compose(
            stage=item["stage"], name=item["name"], kind=item["kind"],
            label=item["label"], when=item["when"], age=item["age"],
            ideas=item["ideas"], past_gifts=item["past_gifts"],
        )
        # Claim the slot first so a concurrent run cannot also send it.
        try:
            conn.execute(
                "INSERT INTO reminder_log (important_date_id, year, stage, sent_at) "
                "VALUES (?,?,?,?)",
                (item["date_id"], item["when"].year, item["stage"], now()),
            )
            conn.commit()
        except Exception:
            logger.info("already claimed: %s %s", item["name"], item["stage"])
            continue

        if sms.send(body, scope=f"date-{item['stage']}", day=today.isoformat()):
            sent += 1
            logger.info("sent %s for %s", item["stage"], item["name"])
        else:
            # Give the slot back — a relay outage should not consume the
            # reminder permanently.
            conn.execute(
                "DELETE FROM reminder_log WHERE important_date_id=? AND year=? AND stage=?",
                (item["date_id"], item["when"].year, item["stage"]),
            )
            conn.commit()
            logger.error("send failed, released claim: %s %s", item["name"], item["stage"])
    return sent
