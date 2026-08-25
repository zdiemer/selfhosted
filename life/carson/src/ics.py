"""The published calendar feed.

carson never writes to iCloud or Google. It publishes one feed at a secret URL
and both calendar apps subscribe to it — so every device shows carson's events
natively, no write credential exists anywhere, and unsubscribing removes it all
cleanly.

Known limit, and the reason SMS still exists: subscribed-feed refresh is slow
(Google hours, Apple 15 min at best). So this feed carries PLANNED items —
birthdays, anniversaries, confirmed events, follow-up dates — while anything
time-sensitive rides a text.

Written by hand rather than with icalendar/ics.py. The whole surface used here
is VEVENT with DATE or UTC DTSTART and a yearly RRULE; that is the ~80 lines
below, against a dependency to track forever.

The fiddly parts of RFC 5545, all of which break calendar apps silently:
  * CRLF line endings, everywhere, including the last line.
  * Fold at 75 OCTETS (not characters) with a leading space on continuations.
  * Escape backslash, semicolon, comma and newline in TEXT values.
  * All-day events use VALUE=DATE and a non-inclusive DTEND.
  * UIDs must be stable across regenerations or subscribers duplicate events.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

PRODID = "-//zachd//carson//EN"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Fold to 75 octets. Counts UTF-8 bytes, never splitting a character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out: list[str] = []
    chunk = bytearray()
    limit = 75
    for ch in line:
        enc = ch.encode("utf-8")
        if len(chunk) + len(enc) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
            limit = 74  # continuations lose one octet to the leading space
        chunk += enc
    if chunk:
        out.append(chunk.decode("utf-8"))
    return [out[0]] + [" " + part for part in out[1:]]


class Calendar:
    def __init__(self, name: str = "Mr. Carson"):
        self._lines: list[str] = []
        self._begin(name)

    def _add(self, line: str) -> None:
        self._lines.extend(_fold(line))

    def _begin(self, name: str) -> None:
        self._add("BEGIN:VCALENDAR")
        self._add("VERSION:2.0")
        self._add(f"PRODID:{PRODID}")
        self._add("CALSCALE:GREGORIAN")
        self._add("METHOD:PUBLISH")
        self._add(f"X-WR-CALNAME:{_escape(name)}")
        # Apple honours this as a refresh hint; Google ignores it. Costs nothing.
        self._add("X-PUBLISHED-TTL:PT1H")
        self._add("REFRESH-INTERVAL;VALUE=DURATION:PT1H")

    def add_all_day(
        self,
        uid: str,
        summary: str,
        day: date,
        *,
        description: str = "",
        yearly: bool = False,
        stamp: datetime | None = None,
    ) -> None:
        stamp = stamp or datetime.now(timezone.utc)
        self._add("BEGIN:VEVENT")
        self._add(f"UID:{uid}")
        self._add(f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}")
        self._add(f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
        # DTEND is non-inclusive: a one-day event ends the following day.
        self._add(f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}")
        self._add(f"SUMMARY:{_escape(summary)}")
        if description:
            self._add(f"DESCRIPTION:{_escape(description)}")
        if yearly:
            self._add("RRULE:FREQ=YEARLY")
        self._add("TRANSP:TRANSPARENT")
        self._add("END:VEVENT")

    def add_timed(
        self,
        uid: str,
        summary: str,
        start: datetime,
        end: datetime | None = None,
        *,
        description: str = "",
        stamp: datetime | None = None,
    ) -> None:
        stamp = stamp or datetime.now(timezone.utc)
        end = end or (start + timedelta(hours=1))
        self._add("BEGIN:VEVENT")
        self._add(f"UID:{uid}")
        self._add(f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}")
        # Everything is emitted in UTC so the feed needs no VTIMEZONE block.
        self._add(f"DTSTART:{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        self._add(f"DTEND:{end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        self._add(f"SUMMARY:{_escape(summary)}")
        if description:
            self._add(f"DESCRIPTION:{_escape(description)}")
        self._add("END:VEVENT")

    def render(self) -> str:
        return "\r\n".join(self._lines + ["END:VCALENDAR", ""])
