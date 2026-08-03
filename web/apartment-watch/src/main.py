"""apartment-watch — scrape, decide, and either text or stay quiet.

One run: for each enabled source, pull listings, evaluate every one against
criteria.yaml, persist, then send at most one SMS digest of everything that
newly matched.

Two behaviours worth knowing before changing anything here:

* **Silence is meaningful.** A run with no matches sends nothing, so silence has
  to reliably mean "nothing qualified" and never "the parser broke in March".
  That's what the source-health check at the end is defending.
* **Everything is re-evaluated every run**, not just new listings. A unit that
  was too expensive in June and drops in July matches then, and notifies then.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

import config
import geo
import scam
import sources
from fetch import Fetcher
from notify import SmsRelay, format_digest, format_health_alert
from store import Store

logger = logging.getLogger("apartment-watch")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CRITERIA = os.path.join(os.path.dirname(HERE), "criteria.yaml")
DEFAULT_DB = os.environ.get("APARTMENT_WATCH_DB", "/data/apartment-watch.db")


def evaluate(listing, criteria) -> tuple[bool, str | None]:
    """Does this listing qualify? Returns (matched, reject_reason).

    Rules are checked cheapest-first and the *first* failure is what gets
    recorded, so the reject reason in the DB reads as the single most relevant
    thing wrong with a listing rather than a list.
    """
    # Neighborhood, resolved before anything else because it's also what the
    # digest prints.
    hood, how = geo.resolve(listing.lat, listing.lon, listing.stated_neighborhood)
    listing.neighborhood = hood

    # Coordinates are authoritative on whether this is even in the city.
    # Craigslist's SF subarea search leaks Berkeley and out-of-state results,
    # and the source-side slug filter is only a cheap first pass.
    if listing.lat is not None and listing.lon is not None and hood is None:
        return False, "outside SF"

    # Effective rent: base plus whatever parking costs. Parking never gates a
    # match, but a $250/mo garage does count against the budget.
    price = listing.price
    if price is None:
        return False, "no price"

    fee = 0
    has_parking = listing.parking in criteria.parking.accept
    if has_parking:
        if listing.parking_fee:
            fee = listing.parking_fee
        elif criteria.parking.unknown_fee == "exclude":
            return False, "parking cost not stated"
        else:
            fee = criteria.parking.unknown_fee_amount
    listing.effective_price = price + fee

    if listing.effective_price > criteria.search.max_effective_rent:
        return False, f"${listing.effective_price} over ${criteria.search.max_effective_rent}"
    if criteria.search.min_rent and price < criteria.search.min_rent:
        return False, f"${price} under ${criteria.search.min_rent}"

    if listing.bedrooms is None:
        return False, "bedrooms unknown"
    if not (criteria.search.min_bedrooms <= listing.bedrooms <= criteria.search.max_bedrooms):
        return False, f"{listing.bedrooms}br outside range"

    if criteria.laundry.required:
        if listing.laundry in criteria.laundry.reject:
            return False, f"laundry: {listing.laundry}"
        if listing.laundry not in criteria.laundry.accept:
            # 'unknown' lands here. Treating it as a pass would mean texting
            # about units with no laundry, which is the rule we were asked to
            # enforce hardest.
            return False, f"laundry: {listing.laundry}"

    if criteria.parking.required and not has_parking:
        return False, f"parking: {listing.parking}"

    if hood and hood in criteria.exclude_neighborhoods:
        return False, f"neighborhood: {hood} ({how})"

    haystack = f"{listing.title} {listing.body}".lower()
    for keyword in criteria.exclude_keywords:
        if keyword in haystack:
            return False, f"keyword: {keyword}"

    if criteria.scam.enabled:
        # Separate from the scam score: a co-living room isn't fraud, it just
        # isn't an apartment, and it reads as "1br" to every bedroom check.
        if scam.is_room_share(listing):
            return False, "room share, not a unit"
        verdict = scam.evaluate(
            listing,
            threshold=criteria.scam.threshold,
            floors=criteria.scam.price_floors,
        )
        if verdict.is_scam:
            return False, verdict.summary

    return True, None


def run_source(name, fetcher, criteria, store, dry_run: bool) -> tuple[int, int, str | None]:
    """Scrape one source. Returns (seen, matched, error)."""
    try:
        source = sources.build(name)
    except KeyError as exc:
        return 0, 0, str(exc)

    seen_ids = store.seen_ids(name)
    count = matched = 0
    try:
        for listing in source.search(fetcher, criteria, seen_ids):
            count += 1
            ok, reason = evaluate(listing, criteria)
            if ok:
                matched += 1
                logger.info(
                    "  MATCH %-14s $%-5s %-7s %-22s %s",
                    name, listing.effective_price, f"{listing.bedrooms}br",
                    listing.neighborhood or "?", listing.url,
                )
            else:
                logger.debug(
                    "  skip  %-14s $%-5s %-24s %s",
                    name, listing.price, reason, listing.url,
                )
            store.upsert(listing, ok, reason)
        store.commit()
    except Exception as exc:
        # A broken parser costs this source, not the run.
        logger.exception("[%s] failed: %s", name, exc)
        store.commit()
        return count, matched, f"{type(exc).__name__}: {exc}"
    return count, matched, None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily SF rental scraper -> SMS")
    parser.add_argument("--criteria", default=os.environ.get("APARTMENT_WATCH_CRITERIA", DEFAULT_CRITERIA))
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--source", action="append", help="run only this source (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="no SMS, no DB writes — print what would happen")
    parser.add_argument("--force-digest", action="store_true",
                        help="send the digest even with zero new matches (smoke test)")
    parser.add_argument("--no-camoufox", action="store_true",
                        help="cheap HTTP tier only; Craigslist still works, the rest won't")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )
    # Playwright is extremely chatty at DEBUG.
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        criteria = config.load(args.criteria)
    except (OSError, config.ConfigError) as exc:
        logger.error("criteria: %s", exc)
        return 2

    wanted = args.source or criteria.enabled_sources
    if not wanted:
        logger.error("no sources enabled in %s", args.criteria)
        return 2

    if args.dry_run:
        # ":memory:" keeps a dry run from touching real state, which also means
        # every listing looks new — exactly what you want when checking parsers.
        db_path = ":memory:"
        logger.info("dry run: in-memory DB, no SMS")
    else:
        db_path = args.db
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    total_new = 0
    problems: list[str] = []

    with Store(db_path) as store, Fetcher(
        delay_range=criteria.request_delay_seconds,
        use_camoufox=not args.no_camoufox,
    ) as fetcher:
        started = store.start_run()
        logger.info("sources: %s", ", ".join(wanted))

        for name in wanted:
            count, matched, error = run_source(name, fetcher, criteria, store, args.dry_run)
            health = store.record_source(name, count, error)
            store.commit()

            if error:
                logger.error("[%s] %d listings, ERROR: %s", name, count, error)
                problems.append(f"{name}: {error.split(':')[0]}")
            else:
                logger.info("[%s] %d listings, %d matched", name, count, matched)
                if count == 0 and health.consecutive_empty >= criteria.alerts.health_alert_after_stale_runs:
                    problems.append(f"{name}: 0 listings for {health.consecutive_empty} runs")

        logger.info("fetch stats: %s", fetcher.stats)

        pending = store.pending_notifications()
        total_new = len(pending)
        date_label = date.today().strftime("%-m/%-d")
        relay = SmsRelay(dry_run=args.dry_run)
        sent = False
        health_alerted = False

        if pending:
            body = format_digest(
                pending, total_new, criteria.alerts.max_listings_per_digest,
                date_label, problems,
            )
            sent = relay.send(criteria.alerts.to, body, f"apartment-watch:{date.today().isoformat()}")
            if sent:
                store.mark_notified(pending)
                store.commit()
        elif args.force_digest:
            sent = relay.send(
                criteria.alerts.to,
                f"apartment-watch {date_label} — smoke test, 0 new matches. "
                f"Sources: {', '.join(wanted)}.",
                f"apartment-watch:smoke:{date.today().isoformat()}",
            )
        elif problems and store.health_alert_allowed(criteria.alerts.health_alert_cooldown_days):
            # Zero matches *and* something looks broken. This is the only case
            # where we break silence without a listing to show for it.
            logger.warning("no matches and sources look unhealthy: %s", problems)
            sent = relay.send(
                criteria.alerts.to,
                format_health_alert(problems, date_label),
                f"apartment-watch:health:{date.today().isoformat()}",
            )
            # Only a health alert we actually sent starts the cooldown. Marking
            # the run otherwise would suppress the next three days of alerts
            # over a text that never went out.
            health_alerted = sent
        else:
            logger.info("no new matches — staying quiet")

        store.finish_run(started, total_new, total_new if sent else 0, health_alerted)

    logger.info("done: %d new match(es)", total_new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
