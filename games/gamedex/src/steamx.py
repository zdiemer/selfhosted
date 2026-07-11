"""Steam-appid-keyed extras: Deck compatibility, ProtonDB, SteamSpy, achievements.

All four key off the Steam appid, which IGDB's external_games already gave us for
6,088 games. That makes this an EXACT lookup like ArcadeDB's MAME romset — it
cannot mismatch, so there's no validator here at all.

  * Deck compatibility — Valve's own endpoint. Verified / Playable / Unsupported.
  * ProtonDB — the community's Linux/Deck tier (platinum…borked) and score.
  * SteamSpy — owner estimates, the review split, concurrent players.
  * Achievements — Valve's global completion percentages; we keep the count, the
    rarest one, and the median, which is a decent "how grindy is 100%?" signal.

One game costs four requests, so the client is rate-limited as a whole rather
than per-endpoint. A failure in any one of them is non-fatal: the record just
carries whatever did come back.
"""

from __future__ import annotations

import logging
import statistics

import requests

from igdb import RateLimiter

log = logging.getLogger("gamedex.steamx")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_DECK = {0: "Unknown", 1: "Unsupported", 2: "Playable", 3: "Verified"}


class SteamExtraClient:
    def __init__(self):
        self._limiter = RateLimiter(1)      # 4 calls per game; be a good citizen

    @property
    def configured(self):
        return True

    def _get(self, url, params=None, timeout=20):
        self._limiter.wait()
        r = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # -- the four endpoints --------------------------------------------------
    def _deck(self, appid):
        try:
            j = self._get("https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport",
                          {"nAppID": appid, "l": "english"})
            cat = ((j or {}).get("results") or {}).get("resolved_category")
            return _DECK.get(cat) if cat else None
        except Exception as exc:
            log.debug("deck %s: %s", appid, exc)
            return None

    def _proton(self, appid):
        try:
            j = self._get(f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json")
            if not j:
                return None
            return {"tier": j.get("tier"), "score": j.get("score"), "reports": j.get("total")}
        except Exception as exc:
            log.debug("protondb %s: %s", appid, exc)
            return None

    def _steamspy(self, appid):
        try:
            j = self._get("https://steamspy.com/api.php", {"request": "appdetails", "appid": appid})
            if not j or not j.get("name"):
                return None
            pos, neg = j.get("positive") or 0, j.get("negative") or 0
            total = pos + neg
            return {
                "owners": j.get("owners") or None,
                "positive": pos, "negative": neg,
                "reviewScore": round(pos / total, 4) if total else None,
                "concurrent": j.get("ccu") or None,
            }
        except Exception as exc:
            log.debug("steamspy %s: %s", appid, exc)
            return None

    def _achievements(self, appid):
        try:
            j = self._get("https://api.steampowered.com/ISteamUserStats/"
                          "GetGlobalAchievementPercentagesForApp/v2/", {"gameid": appid})
            ach = ((j or {}).get("achievementpercentages") or {}).get("achievements") or []
            if not ach:
                return None
            pcts = [float(a.get("percent") or 0) for a in ach]
            rarest = min(ach, key=lambda a: float(a.get("percent") or 0))
            return {
                "count": len(ach),
                "medianPercent": round(statistics.median(pcts), 2),
                "rarest": rarest.get("name"),
                "rarestPercent": round(float(rarest.get("percent") or 0), 2),
            }
        except Exception as exc:
            log.debug("achievements %s: %s", appid, exc)
            return None

    # -- enricher entry points ----------------------------------------------
    def match_meta(self, meta):
        """The enricher hands us the appid it pulled off the IGDB record."""
        appid = meta.get("steamAppId")
        return self.match_appid(appid) if appid else None

    def match(self, title, platform=None, year=None):
        return None                          # appid-only; see match_meta

    def override_from_url(self, title, url):
        import re
        m = re.search(r"/app/(\d+)", url or "")
        return self.match_appid(m.group(1)) if m else None

    def match_appid(self, appid):
        appid = str(appid)
        deck = self._deck(appid)
        proton = self._proton(appid)
        spy = self._steamspy(appid)
        ach = self._achievements(appid)
        if not any((deck, proton, spy, ach)):
            return None
        rec = {
            "appid": appid,
            "url": f"https://store.steampowered.com/app/{appid}/",
            "deck": deck,
            "protonUrl": f"https://www.protondb.com/app/{appid}",
        }
        if proton:
            rec["protonTier"] = proton["tier"]
            rec["protonScore"] = proton["score"]
            rec["protonReports"] = proton["reports"]
        if spy:
            rec.update(spy)
        if ach:
            rec["achievements"] = ach
        return rec
