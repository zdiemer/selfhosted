"""The Notes column, unpacked into facetable fields.

Notes is one free-text cell doing eight jobs at once: it is where the sheet
records which storefront a copy came from, which subscription it arrived through,
which boutique label pressed the cartridge, which edition sits on the shelf,
whether the game has been delisted, whether the disc is cracked, and which
accessory it needs. 5,590 rows use it, across 147 distinct values.

Ported from ExcelGame.__process_notes in zdiemer/GamesMaster. The vocabularies and
the ORDER are both copied deliberately: the original returns as soon as a value
matches a category, so every Notes value lands in exactly one bucket. Reordering
these checks would silently reclassify rows — "Super NES Classic Edition" is a
digital platform, and only stays one because that check runs before the generic
"...Edition" rule.
"""

from __future__ import annotations

# The value IS the category. Matched exactly, not fuzzily.
DIGITAL_PLATFORM = {
    "32-bit iOS", "Abandonware", "Amazon", "Battle.net", "Desura", "DRM Free",
    "Epic Games Store", "Freeware", "GOG", "Green Man Gaming", "Humble Bundle",
    "itch.io", "Johren", "Legacy Games", "Mojang", "Net Yaroze",
    "Nintendo 3DS Ambassador Program", "Oculus", "Origin", "Other", "Pirated",
    "Playdate", "Playdate Catalog", "Playdate Season 1", "Playdate Season 2",
    "Square Enix", "Steam", "Super NES Classic Edition", "Twitch", "uPlay",
    "Virtual Console", "Xbox Live Indie Games",
}
SUBSCRIPTION_SERVICE = {
    "Apple Arcade", "Games with Gold", "Netflix Games", "Nintendo Switch Online",
    "OnLive", "PlayStation Plus", "Stadia Pro", "Viveport", "Xbox Game Pass",
}
LIMITED_PRINT_COMPANY = {
    "Fangamer", "Hard Copy Games", "iam8bit", "Limited Rare Games",
    "Limited Run Games", "PixelHeart", "Play-Asia Exclusive",
    "Special Reserve Games", "Strictly Limited Games", "Super Rare Games",
}
PHYSICAL_MEDIA_FORMAT = {"LaserDisc"}
REQUIRED_ACCESSORY = {
    "Adventure Player", "Nintendo Power", "Starpath Supercharger", "Super Scope",
}


def process(notes) -> dict:
    """Notes -> the fields it encodes. Same early-return order as GamesMaster."""
    if not notes:
        return {}
    n = str(notes).strip()
    if not n:
        return {}

    if n in DIGITAL_PLATFORM:
        return {"digitalPlatform": n}

    if n in SUBSCRIPTION_SERVICE:
        return {"subscription": n}

    if n == "Delisted":
        return {"delisted": True}

    if n in LIMITED_PRINT_COMPANY:
        return {"limitedPrint": n}

    out: dict = {}
    # "Limited Run Games - Dual Pack with ..." — the label, plus whatever is left.
    if n.startswith("Limited Run Games"):
        out["limitedPrint"] = "Limited Run Games"
        n = n.replace("Limited Run Games", "").replace(" - ", "").strip()
        if not n:
            return out

    if n in PHYSICAL_MEDIA_FORMAT:
        out["physicalMedia"] = n
        return out

    if n == "Link":
        return out                       # a URL; nothing to categorise

    if n in REQUIRED_ACCESSORY:
        out["requiredAccessory"] = n
        return out

    # "Gray and gold copies" -> two copies, two variants.
    if " and " in n:
        copies = n.replace(" copies", "").split(" and ")
        out["copiesOwned"] = len(copies)
        out["edition"] = copies[0]
        return out

    if n.endswith("Edition"):
        out["edition"] = n
        return out

    if n.startswith("Collection with") or n.startswith("Dual Pack") or n.endswith("Trilogy"):
        out["multiDiscCollection"] = n
        return out

    low = n.casefold()
    if "broken" in low or "damage" in low or "poor" in low:
        out["damaged"] = True
        # "Damaged case, UAE / Saudi Arabia ... edition" — the tail is the variant.
        parts = n.split(", ")
        if len(parts) > 1:
            out["edition"] = parts[-1]
        return out

    return out
