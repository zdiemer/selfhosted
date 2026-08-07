"""Where this process's outbound traffic leaves from.

One module, because this run makes external requests through three different
clients and they do not agree on how a proxy is configured:

  * httpx     (fetch.py, the cheap tier)  -- takes a URL string
  * Camoufox  (fetch.py, the browser tier) -- takes a Playwright dict, and
                                              REJECTS inline credentials
  * urllib    (liveness.py)                -- takes an opener

Getting one of them wrong is not a visible failure. It is a split egress: part
of the run leaves from one address and part from another, which for an anti-bot
target is worse than either address on its own. The sibling smitele-bot repo
learned this the expensive way -- a Cloudflare clearance cookie minted through a
browser on one exit and replayed by an HTTP client on another is refused every
time, and each refusal costs a solve out of a daily budget of twelve.

WHY THIS READS ITS OWN VARIABLE INSTEAD OF HTTP_PROXY. Because the split above
is exactly what HTTP_PROXY would cause here. urllib and httpx both honour the
environment; Camoufox does not. Setting HTTP_PROXY would silently route the
cheap tier and the liveness sweep through the proxy while the browser tier --
the one that actually faces PerimeterX and Akamai -- kept leaving direct.

It would also capture traffic that must NOT be proxied: notify.py posts to
sms-relay inside the cluster over urllib, and that has no business crossing a
forward proxy. A NO_PROXY list can express that, but it is one typo away from
breaking SMS with no error anyone would look at.

So the chart opts in with `egress.proxy.mode: explicit`, no HTTP_PROXY is ever
set, and every caller that should be proxied says so by using this module.
Anything that does not is direct by construction rather than by omission.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

ENV_VAR = "EGRESS_PROXY_URL"
DIRECT = "direct"


def proxy_url() -> str | None:
    """The configured proxy, or None for direct egress."""
    value = (os.environ.get(ENV_VAR) or "").strip()
    return value or None


def identity(url: str | None = None) -> str:
    """A credential-free name for the exit, safe to log.

    `http://watch:hunter2@egress-proxy.infra.svc.cluster.local:3128`
        -> `http://egress-proxy.infra.svc.cluster.local:3128`

    A configured-but-unparseable value returns "proxy" rather than falling back
    to "direct", so a typo can never be mistaken in a log for the absence of a
    proxy.
    """
    url = url if url is not None else proxy_url()
    if not url:
        return DIRECT
    try:
        p = urllib.parse.urlsplit(url)
        if not p.hostname:
            return "proxy"
        host = f"{p.hostname}:{p.port}" if p.port else p.hostname
        return f"{p.scheme}://{host}"
    except ValueError:
        return "proxy"


def httpx_proxy() -> str | None:
    """Value for httpx.Client(proxy=...). httpx accepts inline credentials."""
    return proxy_url()


def camoufox_proxy() -> dict | None:
    """Value for Camoufox(proxy=...), which is Playwright's shape.

    Playwright rejects `scheme://user:pass@host:port` outright, so the
    credentials have to be split out into their own keys.
    """
    url = proxy_url()
    if not url:
        return None
    p = urllib.parse.urlsplit(url)
    if not p.hostname:
        logger.warning("%s is set but unparseable (%r) — browser tier will run direct",
                       ENV_VAR, url)
        return None
    server = f"{p.scheme}://{p.hostname}"
    if p.port:
        server = f"{server}:{p.port}"
    proxy: dict[str, str] = {"server": server}
    if p.username:
        proxy["username"] = urllib.parse.unquote(p.username)
    if p.password:
        proxy["password"] = urllib.parse.unquote(p.password)
    return proxy


_opener: urllib.request.OpenerDirector | None = None


def opener() -> urllib.request.OpenerDirector:
    """A urllib opener for EXTERNAL requests, proxied if one is configured.

    When no proxy is configured this still installs an empty ProxyHandler,
    which makes urllib ignore the environment. That is deliberate: it means the
    egress path is decided here and only here, and cannot be changed by a stray
    variable in the pod's environment.
    """
    global _opener
    if _opener is None:
        url = proxy_url()
        handler = urllib.request.ProxyHandler({"http": url, "https": url} if url else {})
        _opener = urllib.request.build_opener(handler)
    return _opener


def describe() -> str:
    """One line for the run log, so every run records where it left from."""
    return f"egress: {identity()}"
