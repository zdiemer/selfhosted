"""Drop listings that have been removed before they're texted about.

Craigslist posts get deleted by their author or flagged down by users, often
within hours — bait especially. Nothing else in the pipeline notices: a listing
is scraped once, `Store.hydrate` merges its stored detail back in on every
later run, and it keeps matching forever. So a post that died on Monday could
still be in Thursday's digest, and tapping it lands on "this posting has been
flagged for removal".

Checked at send time rather than at scrape time, because the gap that matters
is between matching and being read, and because `pending` is a handful of rows
where the full listing table is hundreds.

**Only 404 and 410 count as removed.** A 403 means a bot wall, and a timeout
means the network — treating either as "gone" would quietly delete good
listings the first time Zillow felt grumpy. Anything that isn't an explicit
"this is not here" is left alone.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Statuses that mean the post itself is gone. Craigslist answers 410 for both
# author-deleted and community-flagged posts.
DEAD_STATUSES = {404, 410}

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


def _status(url: str, timeout: int = 12) -> int | None:
    """HTTP status for a listing URL, or None if we couldn't tell."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        logger.debug("liveness: %s unreachable (%s)", url, exc)
        return None


def prune_dead(store, rows, sources: set[str] | None = None) -> list:
    """Return the rows still live, marking the dead ones in the database.

    `sources` limits which sources are checked — the browser-walled ones answer
    403 to a plain request, so asking them tells us nothing and costs a request
    each. Craigslist is the one that both matters and answers honestly.
    """
    keep, dropped = [], 0
    for row in rows:
        if sources is not None and row["source"] not in sources:
            keep.append(row)
            continue
        status = _status(row["url"])
        if status in DEAD_STATUSES:
            dropped += 1
            store.mark_dead(row["source"], row["external_id"], f"removed (HTTP {status})")
            logger.info("  removed  %-12s %s", row["source"], row["url"])
            continue
        keep.append(row)
    if dropped:
        store.commit()
        logger.info("liveness: dropped %d removed listing(s) before sending", dropped)
    return keep
