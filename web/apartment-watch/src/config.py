"""criteria.yaml -> dataclasses, validated once at startup.

Everything the user is expected to tune lives in criteria.yaml, mounted from a
ConfigMap. Validation is deliberately strict and happens before any network
call: a typo in the config should fail the run in the first second with a clear
message, not silently widen the search and text you about a $4000 studio in the
Tenderloin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

# Laundry/parking are normalized by each source into these tokens, so criteria
# can be written once and mean the same thing across four very different sites.
LAUNDRY_TOKENS = {"in_unit", "in_building", "on_site", "hookups_only", "none", "unknown"}
PARKING_TOKENS = {"garage", "carport", "off_street", "valet", "street", "none", "unknown"}


class ConfigError(ValueError):
    """Raised for anything wrong in criteria.yaml. Fatal by design."""


@dataclass(frozen=True)
class Search:
    city: str
    min_bedrooms: int
    max_bedrooms: int
    max_effective_rent: int
    min_rent: int = 0


@dataclass(frozen=True)
class LaundryRule:
    required: bool
    accept: frozenset[str]
    reject: frozenset[str]


@dataclass(frozen=True)
class ParkingRule:
    required: bool
    accept: frozenset[str]
    # What to do when a listing has parking but never says what it costs.
    #   included      -> add $0 (assume it's in the rent)
    #   assume:<n>    -> add $n, the conservative reading
    #   exclude       -> drop the listing rather than guess
    unknown_fee: str

    @property
    def unknown_fee_amount(self) -> int:
        if self.unknown_fee.startswith("assume:"):
            return int(self.unknown_fee.split(":", 1)[1])
        return 0


@dataclass(frozen=True)
class Alerts:
    to: str
    max_listings_per_digest: int
    max_messages_per_run: int
    # Local hours at which a digest may be sent. Scraping runs far more often
    # than this: polling hourly finds a listing sooner, it does not create more
    # matches (dedup means each texts once, ever), so the two cadences are
    # deliberately separate. Empty list = send on every run.
    send_hours: tuple[int, ...]
    timezone: str
    seconds_between_messages: int
    web_base_url: str
    quiet_on_zero_matches: bool
    health_alert_after_stale_runs: int
    health_alert_cooldown_days: int


@dataclass(frozen=True)
class ScamFilter:
    enabled: bool
    threshold: int
    market_rent: dict
    bait_ratio: float
    premium_ratio: float
    live_market: bool
    market_refresh_days: int
    price_floors: dict | None


@dataclass(frozen=True)
class Criteria:
    search: Search
    laundry: LaundryRule
    parking: ParkingRule
    scam: ScamFilter
    exclude_neighborhoods: frozenset[str]
    exclude_keywords: tuple[str, ...]
    sources: dict[str, dict[str, Any]]
    alerts: Alerts
    daily_source_hour: int = 9
    rent_control: bool = True
    geocode_addresses: bool = True
    max_detail_fetches: int = 40
    request_delay_seconds: tuple[float, float] = (1.5, 4.0)

    @property
    def enabled_sources(self) -> list[str]:
        return [name for name, cfg in self.sources.items() if cfg.get("enabled")]


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return d[key]


def _tokens(raw: Any, allowed: set[str], where: str) -> frozenset[str]:
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: expected a list, got {type(raw).__name__}")
    bad = set(raw) - allowed
    if bad:
        raise ConfigError(
            f"{where}: unknown value(s) {sorted(bad)}. Allowed: {sorted(allowed)}"
        )
    return frozenset(raw)


def load(path: str) -> Criteria:
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    s = _require(raw, "search", "criteria.yaml")
    search = Search(
        city=s.get("city", "san-francisco"),
        min_bedrooms=int(_require(s, "min_bedrooms", "search")),
        max_bedrooms=int(_require(s, "max_bedrooms", "search")),
        max_effective_rent=int(_require(s, "max_effective_rent", "search")),
        min_rent=int(s.get("min_rent", 0)),
    )
    if search.min_bedrooms > search.max_bedrooms:
        raise ConfigError("search: min_bedrooms is greater than max_bedrooms")
    if search.max_effective_rent <= 0:
        raise ConfigError("search: max_effective_rent must be positive")

    rules = raw.get("rules", {})
    lr = rules.get("laundry", {})
    laundry = LaundryRule(
        required=bool(lr.get("required", True)),
        accept=_tokens(lr.get("accept", []), LAUNDRY_TOKENS, "rules.laundry.accept"),
        reject=_tokens(lr.get("reject", []), LAUNDRY_TOKENS, "rules.laundry.reject"),
    )
    if laundry.accept & laundry.reject:
        raise ConfigError(
            f"rules.laundry: {sorted(laundry.accept & laundry.reject)} is in both "
            "accept and reject"
        )

    pr = rules.get("parking", {})
    unknown_fee = str(pr.get("unknown_fee", "included"))
    if unknown_fee != "included" and unknown_fee != "exclude":
        if not re.fullmatch(r"assume:\d+", unknown_fee):
            raise ConfigError(
                "rules.parking.unknown_fee must be 'included', 'exclude', or "
                f"'assume:<dollars>' — got {unknown_fee!r}"
            )
    parking = ParkingRule(
        required=bool(pr.get("required", False)),
        accept=_tokens(pr.get("accept", []), PARKING_TOKENS, "rules.parking.accept"),
        unknown_fee=unknown_fee,
    )

    alerts_raw = raw.get("alerts", {})
    to = str(_require(alerts_raw, "to", "alerts"))
    if not re.fullmatch(r"\+?[0-9][0-9\-(). ]{6,}", to):
        raise ConfigError(f"alerts.to does not look like a phone number: {to!r}")
    alerts = Alerts(
        to=to,
        max_listings_per_digest=int(alerts_raw.get("max_listings_per_digest", 5)),
        max_messages_per_run=max(1, int(alerts_raw.get("max_messages_per_run", 3))),
        send_hours=tuple(sorted({int(h) for h in (alerts_raw.get("send_hours") or [])})),
        timezone=str(alerts_raw.get("timezone", "America/Los_Angeles")),
        seconds_between_messages=max(0, int(alerts_raw.get("seconds_between_messages", 20))),
        web_base_url=str(alerts_raw.get("web_base_url", "https://homes.diemer.codes")).rstrip("/"),
        quiet_on_zero_matches=bool(alerts_raw.get("quiet_on_zero_matches", True)),
        health_alert_after_stale_runs=int(alerts_raw.get("health_alert_after_stale_runs", 3)),
        health_alert_cooldown_days=int(alerts_raw.get("health_alert_cooldown_days", 3)),
    )

    sources = raw.get("sources", {})
    if not isinstance(sources, dict) or not sources:
        raise ConfigError("sources: at least one source must be configured")

    for h in alerts.send_hours:
        if not 0 <= h <= 23:
            raise ConfigError(f"alerts.send_hours: {h} is not an hour of the day")

    delay = raw.get("request_delay_seconds", [1.5, 4.0])
    if not (isinstance(delay, list) and len(delay) == 2 and delay[0] <= delay[1]):
        raise ConfigError("request_delay_seconds must be [min, max] with min <= max")

    import scam as scam_mod

    scam_raw = raw.get("scam_filter", {}) or {}
    bait = float(scam_raw.get("bait_ratio", scam_mod.DEFAULT_BAIT_RATIO))
    premium = float(scam_raw.get("premium_ratio", scam_mod.DEFAULT_PREMIUM_RATIO))
    if not 0 < bait <= 1:
        raise ConfigError("scam_filter.bait_ratio must be between 0 and 1")
    if not bait <= premium <= 1:
        raise ConfigError("scam_filter.premium_ratio must be between bait_ratio and 1")
    scam_cfg = ScamFilter(
        enabled=bool(scam_raw.get("enabled", True)),
        threshold=int(scam_raw.get("threshold", scam_mod.DEFAULT_THRESHOLD)),
        market_rent=scam_mod.int_map_from_config(
            scam_raw.get("market_rent"), scam_mod.DEFAULT_MARKET_RENT
        ),
        bait_ratio=bait,
        premium_ratio=premium,
        live_market=bool((scam_raw.get("market_rent_live") or {}).get("enabled", True)),
        market_refresh_days=max(1, int((scam_raw.get("market_rent_live") or {}).get("refresh_days", 30))),
        price_floors=(
            scam_mod.int_map_from_config(scam_raw.get("price_floors"), {})
            if scam_raw.get("price_floors") else None
        ),
    )

    return Criteria(
        search=search,
        laundry=laundry,
        parking=parking,
        scam=scam_cfg,
        exclude_neighborhoods=frozenset(raw.get("exclude_neighborhoods") or []),
        exclude_keywords=tuple(
            k.lower() for k in (raw.get("exclude_keywords") or [])
        ),
        sources=sources,
        alerts=alerts,
        daily_source_hour=int(raw.get("daily_source_hour", (alerts.send_hours or [9])[0])),
        rent_control=bool(raw.get("rent_control", True)),
        geocode_addresses=bool(raw.get("geocode_addresses", True)),
        max_detail_fetches=int(raw.get("max_detail_fetches", 40)),
        request_delay_seconds=(float(delay[0]), float(delay[1])),
    )
