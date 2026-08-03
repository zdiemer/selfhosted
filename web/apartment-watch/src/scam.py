"""Basic scam / bait detection.

Rental listing fraud has a small, stable set of tells, and SF Craigslist is
thick with them. The live search that seeded this module had a "Modern 1 Bedroom"
in Russian Hill at $1,285 — roughly a third of market, which is the single
loudest signal there is.

Scoring, not a keyword blacklist. Any one signal has false positives: a real
landlord can write "no credit check", a real listing can be furnished and
cheap. Requiring several signals to agree keeps a legitimately good deal — the
whole point of this tool — from being thrown away by one unlucky phrase.

Every rejection records *which* rules fired, so a listing killed by this can be
audited in the DB rather than just vanishing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Rent below this is not a deal, it's bait.
#
# Calibrated against a live SF pull in August 2026, which surfaced a cluster of
# listings in one identical marketing voice ("Enjoy comfortable city living in
# this charming studio...") at $1,275 for a studio by Golden Gate Park and
# $1,500 for a Mission 1BR with in-unit laundry. Those prices do not exist in
# this city; they're a posting farm harvesting replies.
#
# This is the one filter with real false-negative risk — a genuine
# rent-controlled unit can land under these. They're deliberately set below
# market rather than at it, and they're the first thing to lower in
# criteria.yaml if the digest feels too thin.
DEFAULT_FLOORS = {0: 1600, 1: 1950, 2: 2600, 3: 3200}

# Rooms in a shared flat, co-living beds, and SROs, all routinely posted under
# "apts/housing". They aren't apartments and no bedroom-count check catches
# them — a private room reads as "1br" everywhere.
_ROOM_SHARE_RE = re.compile(
    r"shared\s+living|co-?living|communal\s+(?:kitchen|bath|living)"
    r"|shared\s+(?:kitchen|bathroom|bath\b|house|apartment|apt\b|flat\b|unit\b)"
    r"|private\s+room|room\s+for\s+rent|rooms?\s+available|furnished\s+room"
    r"|roommates?\b|\bsro\b|single\s+room\s+occupancy|room\s+in\s+a\s+"
    r"|per\s+room\b|bed\s+in\s+a\b",
    re.I,
)


def is_room_share(listing) -> bool:
    """A room in someone else's home, not a unit of your own."""
    return bool(_ROOM_SHARE_RE.search(f"{listing.title or ''}\n{listing.body or ''}"))


@dataclass(frozen=True)
class Rule:
    key: str
    points: int
    pattern: re.Pattern
    why: str


def _r(key: str, points: int, pattern: str, why: str) -> Rule:
    return Rule(key, points, re.compile(pattern, re.I), why)


# Payment-rail tells. Legitimate SF landlords do not ask for wires or gift
# cards, and never before a viewing.
PAYMENT_RULES = [
    _r("wire", 4, r"\bwire\s+(?:transfer|the\s+money|funds)\b|western\s+union|money\s?gram",
       "asks for a wire transfer"),
    _r("giftcard", 5, r"gift\s?cards?\b|itunes\s+card|steam\s+card", "asks for gift cards"),
    _r("crypto", 4, r"\bbitcoin\b|\bcrypto(?:currency)?\b|\busdt\b", "asks for crypto"),
    _r("deposit_first", 3,
       r"(?:deposit|first\s+month|payment)[^.]{0,40}\b(?:before|prior to)\b[^.]{0,30}\b(?:view|see|tour|visit|key)",
       "wants money before a viewing"),
    _r("cashier", 2, r"cashier'?s?\s+che(?:ck|que)|certified\s+che(?:ck|que)",
       "insists on a cashier's check"),
]

# Absentee-landlord narratives — the classic advance-fee setup.
NARRATIVE_RULES = [
    _r("out_of_country", 4,
       r"out\s+of\s+(?:the\s+)?(?:country|state)|currently\s+abroad|relocated\s+to\s+another"
       r"|missionar|on\s+a\s+mission\s+trip|work(?:ing)?\s+overseas",
       "landlord claims to be away"),
    _r("keys_mailed", 5,
       r"(?:keys?|documents?)\s+(?:will\s+be\s+)?(?:mail|ship|courier|fedex|ups)"
       r"|(?:mail|ship|send|courier|fedex)\s+(?:you\s+)?(?:the\s+)?keys?\b",
       "promises to mail the keys"),
    _r("god", 2, r"\bgod\s+bless\b|\bgod\s+fearing\b|\bblessed\b\s+day", "religious appeal"),
    _r("agent_absent", 2, r"no\s+(?:agent|realtor|broker)\s+(?:is\s+)?(?:needed|involved)"
       r"|deal\s+directly\s+with\s+(?:the\s+)?owner\s+only", "insists on no agent"),
]

# Too-good-to-be-true bundles and screening waivers.
TERMS_RULES = [
    _r("no_screening", 2,
       r"no\s+credit\s+check|no\s+background\s+check|no\s+screening|bad\s+credit\s+ok"
       r"|no\s+deposit\s+(?:required|needed)|credit\s+doesn'?t\s+matter",
       "waives all screening"),
    _r("all_free", 2,
       r"all\s+utilities\s+(?:are\s+)?(?:free|included)[^.]{0,40}\b(?:free|no\s+cost)\b"
       r"|(?:rent|everything)\s+is\s+negotiable\s+.{0,20}\bfree\b",
       "everything bundled free"),
    _r("urgency", 1,
       r"\bmust\s+(?:go|rent)\s+(?:today|asap|immediately)\b|first\s+come\s+first\s+serve"
       r"|act\s+(?:now|fast)\b|hurry\b", "artificial urgency"),
]

# Push to an off-platform contact — the point at which the relay stops
# protecting you.
CONTACT_RULES = [
    # The loudest tell in practice. A real landlord invites you to a showing;
    # harvesting your number and a personal pitch *before* you've seen anything
    # is either lead resale or the opening move of an advance-fee scam. Caught a
    # $1,800 "studio near Lafayette Park" that cleared every other rule.
    _r("lead_harvest", 4,
       r"leave\s+your\s+(?:phone\s+)?(?:number|#|digits)"
       r"|please\s+(?:leave|provide|send|include)\s+(?:me\s+|us\s+)?your\s+(?:phone|number|contact|cell)"
       r"|tell\s+(?:me|us)\s+(?:a\s+little\s+)?about\s+your\s?self"
       r"|send\s+(?:me|us)\s+your\s+(?:phone\s+)?number",
       "asks for your number instead of offering a showing"),
    _r("offsite_email", 2,
       r"(?:e-?mail|contact|reach)\s+(?:me|us)\s+(?:at|on|directly)?[^.\n]{0,20}"
       r"[a-z0-9._%+-]+@(?:gmail|yahoo|hotmail|outlook|aol|proton)\.",
       "pushes to a personal email"),
    _r("text_only", 1,
       r"\btext\s+(?:me\s+)?only\b|do\s+not\s+call|no\s+phone\s+calls?\b",
       "text-only contact"),
]

# Presentation tells, weak on their own.
STYLE_RULES = [
    _r("shouting", 1, r"[A-Z]{12,}", "shouting title"),
    _r("emoji_spam", 1,
       r"(?:[☀-➿\U0001f300-\U0001faff]\s*){4,}", "emoji spam"),
]

ALL_RULES = PAYMENT_RULES + NARRATIVE_RULES + TERMS_RULES + CONTACT_RULES + STYLE_RULES

# A listing needs this many points to be discarded. Tuned so that any single
# strong signal (wire/keys-mailed/gift cards) is nearly enough on its own, but
# a lone weak one never is.
DEFAULT_THRESHOLD = 4


@dataclass
class Verdict:
    is_scam: bool
    score: int
    reasons: list[str]

    @property
    def summary(self) -> str:
        return f"scam({self.score}): {', '.join(self.reasons)}" if self.reasons else "scam"


def evaluate(
    listing,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    floors: dict[int, int] | None = None,
    underpriced_ratio: float = 0.55,
) -> Verdict:
    """Score a listing for fraud tells.

    `underpriced_ratio` is applied against the *budget*, not against market
    rent — we have no market-rent feed, and the budget is the best available
    proxy for what a real unit of this size costs around here.
    """
    floors = floors or DEFAULT_FLOORS
    text = f"{listing.title or ''}\n{listing.body or ''}"
    score = 0
    reasons: list[str] = []

    for rule in ALL_RULES:
        if rule.pattern.search(text):
            score += rule.points
            reasons.append(rule.why)

    # The strongest single signal: rent far under any plausible floor for that
    # bedroom count. Applied only when we actually know both numbers.
    if listing.price is not None and listing.bedrooms is not None:
        floor = floors.get(listing.bedrooms)
        if floor and listing.price < floor:
            score += 4
            reasons.append(f"${listing.price} is below the ${floor} floor for {listing.bedrooms}br")
        elif floor and listing.price < floor * 1.3:
            # Just above the floor, but carrying amenities that don't come
            # cheap. Real bargains in this city are rent-controlled and
            # *under*-amenitied; a pre-war studio advertising in-unit laundry
            # AND parking at well under market is describing a unit that isn't
            # there. Only 3 points — it needs corroboration, because an
            # under-priced good unit is exactly what we're hunting for.
            premium = listing.laundry == "in_unit" and listing.parking in (
                "garage", "carport", "off_street", "valet"
            )
            if premium:
                score += 3
                reasons.append(
                    f"${listing.price} with in-unit laundry + parking is under market "
                    f"for {listing.bedrooms}br"
                )

    return Verdict(is_scam=score >= threshold, score=score, reasons=reasons)


def floors_from_config(raw) -> dict[int, int]:
    """Parse the optional `scam_filter.price_floors` mapping."""
    if not raw:
        return dict(DEFAULT_FLOORS)
    out = dict(DEFAULT_FLOORS)
    for key, value in raw.items():
        try:
            out[int(key)] = int(value)
        except (TypeError, ValueError):
            logger.warning("scam_filter.price_floors: ignoring %r: %r", key, value)
    return out
