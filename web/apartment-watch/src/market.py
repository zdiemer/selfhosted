"""Live SF market rent, from apartments.com's rent-market-trends page.

The scam filter's price thresholds are a fraction of market rent, so the
quality of every price rejection depends on that number being current. Hard-
coding it means the filter quietly drifts out of calibration as the market
moves — in the wrong direction, too: if rents rise and our number doesn't, the
bait threshold sits lower than it should and more bait gets through.

Refreshed monthly, not daily. Average asking rent moves by single-digit dollars
week to week, and this costs a browser navigation through Akamai.

**A failed fetch must never change matching behaviour.** The order is: cached
value if it's fresh, then a live fetch, then the last cached value however
stale, then the static numbers in criteria.yaml. Every path logs which one it
used, because a silent threshold change is indistinguishable from the market
moving.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

URL = "https://www.apartments.com/rent-market-trends/san-francisco-ca/"
ORIGIN = "https://www.apartments.com"

_LABEL_TO_BEDS = {
    "studio": 0,
    "one bedroom": 1,
    "two bedroom": 2,
    "three bedroom": 3,
    "four bedroom": 4,
}

# Sanity band per bedroom count. A parse that lands outside this is a parse
# error, not a market crash, and must not be allowed to move the thresholds.
_PLAUSIBLE = (800, 25_000)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def parse(html: str) -> dict[int, int] | None:
    """Extract the **Apartments** table's average rent by bedroom count.

    The page renders three tables back to back — Apartments, Houses, Condos —
    with identical markup and no distinguishing class on the rows. Houses and
    condos are much more expensive (a 3br house averages $10.5k against $6k for
    an apartment), so grabbing the wrong one would inflate every threshold.
    Apartments is first, so this reads only up to the second table header.
    """
    if not html:
        return None
    text = _text(html)
    start = text.find("Average Rent by Home Type")
    if start < 0:
        logger.warning("market: 'Average Rent by Home Type' not found — page changed?")
        return None

    segment = text[start:]
    # Each table restarts with this header; keep only the first table's rows.
    headers = [m.start() for m in re.finditer(r"Bedrooms\s+Average Rent", segment)]
    if len(headers) >= 2:
        segment = segment[: headers[1]]

    out: dict[int, int] = {}
    for label, beds in _LABEL_TO_BEDS.items():
        m = re.search(rf"{label}\s*\$\s*([0-9][0-9,]*)\s*/\s*month", segment, re.I)
        if not m:
            continue
        try:
            value = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if _PLAUSIBLE[0] <= value <= _PLAUSIBLE[1]:
            out[beds] = value

    # A studio that costs more than a 1br means we stitched rows from different
    # tables, or the layout changed under us.
    if 0 in out and 1 in out and out[0] >= out[1]:
        logger.warning("market: studio >= 1br (%s) — refusing a bad parse", out)
        return None
    if len(out) < 2:
        logger.warning("market: only parsed %d row(s) — refusing", len(out))
        return None
    return out


def fetch(fetcher) -> dict[int, int] | None:
    """One warmed browser navigation. Returns None on any failure."""
    try:
        html = fetcher.get(URL, stealth_first=True, origin=ORIGIN, warm_up_first=True)
    except Exception as exc:
        logger.warning("market: fetch raised %s", exc)
        return None
    return parse(html)


def resolve(store, fetcher, configured: dict[int, int], refresh_days: int, enabled: bool):
    """The market-rent numbers this run should use, and where they came from."""
    if not enabled:
        return configured, "criteria.yaml (live lookup disabled)"

    cached, age_days = store.market_rent()
    if cached and age_days is not None and age_days < refresh_days:
        return cached, f"cache ({age_days}d old)"

    live = fetch(fetcher) if fetcher is not None else None
    if live:
        store.save_market_rent(live)
        # Worth seeing in the log when it moves: every price rejection is a
        # fraction of these.
        drift = {
            b: f"{configured.get(b, 0)}->{v}"
            for b, v in sorted(live.items())
            if configured.get(b) and abs(v - configured[b]) / configured[b] > 0.02
        }
        if drift:
            logger.info("market: >2%% drift from criteria.yaml: %s", drift)
        return live, "live"

    if cached:
        logger.warning("market: refresh failed, using cache (%sd old)", age_days)
        return cached, f"stale cache ({age_days}d old)"

    logger.warning("market: no live value and no cache — using criteria.yaml")
    return configured, "criteria.yaml (fallback)"
