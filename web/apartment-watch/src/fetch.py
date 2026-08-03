"""Tiered fetching: cheap HTTP first, stealth browser only when blocked.

Same shape as the three-tier fetch in the sibling `money` repo
(backend/money/ingest/scrape.py) — reimplemented rather than imported, since
that's a different repo and a different deployment.

Why tiered: Camoufox is a real Firefox. It costs ~1.5GB of RAM and several
seconds per page. Craigslist answers a plain httpx GET from a residential IP
essentially always; Zillow answers it essentially never. Trying cheap first
means the common case stays fast and the browser only spins up for the sites
that actually need it.

Two hard rules, both learned from `money`:

* Nothing here raises. Every entrypoint returns `str | None`. A blocked fetch
  must degrade to "no listings from this source", never take down the run.
* The browser is started once per run and reused. Launching Camoufox per URL
  is both slow and a good way to get the house IP flagged.
"""

from __future__ import annotations

import logging
import random
import time

import httpx

logger = logging.getLogger(__name__)

# A boring, current desktop UA. Camoufox generates its own fingerprint; this is
# only for the cheap tier.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Body markers that mean "you got a page, but it's a wall". A 200 with one of
# these is a block; treating it as content is how you end up with a parser that
# "works" and returns zero listings forever.
_BLOCK_MARKERS = (
    "px-captcha",
    "perimeterx",
    "captcha-delivery",
    "cf-browser-verification",
    "client challenge",      # Zumper/PadMapper (F5 Shape)
    "just a moment...",
    "enable javascript and cookies to continue",
    "access to this page has been denied",
    "unusual traffic from your computer",
    "request blocked",
)


def looks_blocked(html: str | None) -> bool:
    if not html:
        return True
    if len(html) < 500:
        return True
    lowered = html[:200_000].lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


class Fetcher:
    """One per run. Owns the httpx client and, lazily, the Camoufox browser."""

    def __init__(self, delay_range: tuple[float, float] = (1.5, 4.0), use_camoufox: bool = True):
        self.delay_range = delay_range
        self.use_camoufox = use_camoufox
        self._client = httpx.Client(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        )
        self._browser = None
        self._browser_ctx = None
        self._page = None
        self._browser_failed = False
        self._warmed: set[str] = set()
        self.stats = {"http": 0, "camoufox": 0, "blocked": 0, "failed": 0}

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._browser_ctx is not None:
            try:
                self._browser_ctx.__exit__(None, None, None)
            except Exception as exc:
                logger.warning("camoufox shutdown failed: %s", exc)
            finally:
                self._browser_ctx = None
                self._browser = None

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def sleep(self) -> None:
        """Jittered pause between requests. Politeness, and it looks human."""
        time.sleep(random.uniform(*self.delay_range))

    # -- tiers ------------------------------------------------------------

    def _via_http(self, url: str) -> str | None:
        try:
            resp = self._client.get(url)
        except Exception as exc:
            logger.debug("http GET %s failed: %s", url, exc)
            return None
        if resp.status_code in (403, 429, 503):
            logger.debug("http GET %s -> %d (blocked)", url, resp.status_code)
            return None
        if resp.status_code >= 400:
            logger.debug("http GET %s -> %d", url, resp.status_code)
            return None
        self.stats["http"] += 1
        return resp.text

    def _ensure_browser(self):
        """Start Camoufox once, on first need.

        headless="virtual" runs a real Firefox under Xvfb rather than Firefox's
        own headless mode — headless is itself a detectable signal, and the
        cluster has no display, so Xvfb is the only mode that can ship. geoip
        aligns timezone/locale/WebGL with the exit IP and humanize adds cursor
        movement; that's the combination already working against PerimeterX in
        the sibling `money` repo.

        uBlock Origin is excluded deliberately. Camoufox downloads it at launch,
        which costs startup time and writes into the browser cache — and in the
        pod that cache lives on a read-only filesystem, so the write fails. It
        does nothing for scraping.
        """
        if self._browser is not None or self._browser_failed or not self.use_camoufox:
            return self._browser
        try:
            from camoufox.sync_api import Camoufox
            from camoufox import DefaultAddons
        except Exception as exc:
            logger.warning("camoufox unavailable (%s) — cheap tier only", exc)
            self._browser_failed = True
            return None
        try:
            self._browser_ctx = Camoufox(
                headless="virtual",
                geoip=True,
                humanize=True,
                exclude_addons=[DefaultAddons.UBO],
            )
            self._browser = self._browser_ctx.__enter__()
            logger.info("camoufox started")
        except Exception as exc:
            logger.warning("camoufox failed to start (%s) — cheap tier only", exc)
            self._browser_ctx = None
            self._browser = None
            self._browser_failed = True
        return self._browser

    def _ensure_page(self):
        """One long-lived page for the whole run.

        This is load-bearing, not an optimisation. In Playwright,
        `browser.new_page()` creates a *fresh isolated context* — new cookie
        jar, no history. Akamai and PerimeterX both score a cold deep-link far
        worse than a second navigation inside a warmed session, so a page per
        URL throws away exactly the state that gets us through the wall.
        """
        browser = self._ensure_browser()
        if browser is None:
            return None
        if self._page is None:
            self._page = browser.new_page()
        return self._page

    def warm_up(self, origin: str, dwell_ms: int = 12_000) -> None:
        """Land on a site's homepage and linger before asking for real content.

        Apartments.com is the clearest case: a cold GET of a search URL returns
        a 2.5KB Akamai challenge every time, but loading the homepage, waiting
        for the sensor to post, and *then* navigating to the search page returns
        the real 800KB listing page. Once per origin per run.
        """
        if origin in self._warmed:
            return
        page = self._ensure_page()
        if page is None:
            return
        self._warmed.add(origin)
        try:
            logger.info("warming up %s", origin)
            page.goto(origin, timeout=90_000, wait_until="domcontentloaded")
            page.wait_for_timeout(dwell_ms)
            # A little cursor movement; these sensors score "no pointer events
            # ever" as non-human.
            for x, y in ((320, 380), (700, 500), (900, 300)):
                try:
                    page.mouse.move(x, y)
                    page.wait_for_timeout(600)
                except Exception:
                    break
        except Exception as exc:
            logger.debug("warm-up of %s failed: %s", origin, exc)

    def _via_camoufox(self, url: str, wait_ms: int = 8000, wait_for: str | None = None) -> str | None:
        page = self._ensure_page()
        if page is None:
            return None
        try:
            resp = page.goto(url, timeout=90_000, wait_until="domcontentloaded")
            if resp is not None and resp.status >= 400:
                logger.debug("camoufox %s -> %d", url, resp.status)
                return None
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=wait_ms)
                except Exception:
                    # Selector never appeared — return whatever rendered and let
                    # the parser decide. Often still enough.
                    pass
            else:
                page.wait_for_timeout(wait_ms)
            html = page.content()
            self.stats["camoufox"] += 1
            return html
        except Exception as exc:
            logger.debug("camoufox %s failed: %s", url, exc)
            return None

    # -- public -----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        stealth_first: bool = False,
        wait_for: str | None = None,
        origin: str | None = None,
        warm_up_first: bool = False,
    ) -> str | None:
        """Fetch a page, escalating as far as it needs to and no further.

        Three tiers, cheapest first:

          1. plain httpx           — Craigslist lives here
          2. Camoufox, cold        — Zumper and Zillow live here
          3. Camoufox, warmed      — Apartments.com lives here

        `stealth_first=True` skips tier 1 for a host known to sit behind a bot
        wall. `origin` enables the tier-3 retry: if the cold browser fetch comes
        back blocked, warm up that origin and try once more. `warm_up_first`
        goes straight to tier 3 for a site that is *never* served cold —
        Apartments.com returns a 2.5KB Akamai shell every time otherwise.

        Warming is deliberately not the default. It costs ~15s per origin, and
        on a heavy page (Zillow's list page is ~1.9MB) the extra navigation and
        cursor simulation can stall far longer than the fetch it was meant to
        rescue.
        """
        if warm_up_first and origin:
            self.warm_up(origin)

        if not stealth_first:
            html = self._via_http(url)
            if not looks_blocked(html):
                return html
            logger.debug("cheap tier blocked on %s — escalating", url)
            self.stats["blocked"] += 1

        html = self._via_camoufox(url, wait_for=wait_for)
        if not looks_blocked(html):
            return html

        # Last resort: this origin apparently does need a warmed session after
        # all. Only worth one attempt, and only if we haven't already warmed it.
        if origin and origin not in self._warmed:
            logger.info("cold browser fetch blocked on %s — warming up and retrying", url)
            self.warm_up(origin)
            html = self._via_camoufox(url, wait_for=wait_for)
            if not looks_blocked(html):
                return html

        self.stats["failed"] += 1
        return None
