"""Recommendations — "because you liked …".

IGDB hands us a `similar_games` list with every match, and we've been storing it
since the first commit without ever using it. Cross-referenced against the
backlog it becomes a real recommender, with no new data source and no scraping:

    for every game you finished and rated highly
        for every game IGDB says is similar to it
            if that game is sitting in your backlog
                vote for it, and remember why

A game recommended by five things you loved outranks one recommended by one.

Matching is IGDB-name to IGDB-name — the similar list gives canonical IGDB names,
and the backlog rows carry the IGDB name of whatever they matched — so this
sidesteps the messy sheet-title-vs-real-title problem entirely.
"""

from __future__ import annotations

import logging
import math
from collections import Counter

log = logging.getLogger("gamedex.recommend")

MIN_RATING = 0.8         # "liked" = you scored it 80+
MAX_BECAUSE = 3          # reasons listed per recommendation


def _related(seed, cand):
    """Does this pair share anything at all? Genre, franchise or developer."""
    for field in ("genre", "franchise", "developer"):
        a, b = seed.get(field), cand.get(field)
        if a and b and a == b:
            return True
    return False


def build(rows, records, normalize, limit=60):
    """rows: the games sheet · records: {match_key: igdb record} · normalize: str -> str"""
    # Index the backlog by the IGDB name of whatever it matched.
    by_name = {}
    for r in rows:
        if r.get("completed") or not r.get("title"):
            continue
        if r.get("playable") == "No":            # can't play it, don't suggest it
            continue
        rec = records.get(r.get("_k"))
        if not rec:
            continue
        name = normalize(rec.get("name") or r["title"])
        if name:
            by_name.setdefault(name, []).append(r)

    # How often does IGDB call each game "similar" to ANYTHING? Its similar_games
    # lists are dominated by a handful of titles that turn up next to everything,
    # so a raw vote count just ranks those (Borderlands 3 came out top, "because
    # you liked Yakuza 0"). Divide by how promiscuous a game is — an IDF weight —
    # and a recommendation has to be specific to you before it counts.
    ubiquity = Counter()
    for rec in records.values():
        for sim in rec.get("similar") or []:
            nm = normalize(sim.get("name") or "")
            if nm:
                ubiquity[nm] += 1

    votes = {}
    seeds = 0
    for r in rows:
        if not r.get("completed") or (r.get("rating") or 0) < MIN_RATING:
            continue
        rec = records.get(r.get("_k"))
        if not rec or not rec.get("similar"):
            continue
        seeds += 1
        # A game you scored 100 vouches harder than one you scored 80.
        strength = (r["rating"] - MIN_RATING) / (1 - MIN_RATING) * 0.5 + 0.5
        for sim in rec["similar"]:
            name = normalize(sim.get("name") or "")
            if not name:
                continue
            weight = strength / math.log(2 + ubiquity.get(name, 1))
            for cand in by_name.get(name, []):
                # IGDB's similar_games is noisy — it happily calls Borderlands 3
                # similar to Zelda. Require the pair to actually have something in
                # common before the vote counts. This is the difference between a
                # recommendation and a non-sequitur.
                if not _related(r, cand):
                    continue
                key = cand.get("_k") or cand["title"]
                v = votes.get(key)
                if v is None:
                    v = votes[key] = {"row": cand, "score": 0.0, "votes": 0, "seeds": set()}
                v["score"] += weight
                v["votes"] += 1
                v["seeds"].add((r.get("rating") or 0, r["title"]))

    ranked = sorted(votes.values(), key=lambda v: -v["score"])[:limit]

    log.info("recommendations: %d seeds -> %d candidates", seeds, len(votes))
    return {
        "seeds": seeds,
        "items": [
            {
                "key": v["row"].get("_k"),
                "title": v["row"].get("title"),
                "platform": v["row"].get("platform"),
                "year": v["row"].get("releaseYear"),
                "votes": v["votes"],
                "score": round(v["score"], 3),
                # Cite the games you liked MOST, not whichever we happened to see
                # first — the reason IS the recommendation.
                "because": [t for _, t in sorted(v["seeds"], reverse=True)][:MAX_BECAUSE],
            }
            for v in ranked
        ],
    }
