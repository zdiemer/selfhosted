"""Disk-backed asset cache on the PVC.

The UI pulls a very large number of assets straight from third-party servers —
IGDB covers/screenshots/artwork above all, plus GameTDB disc & box scans, VNDB
covers, the Arcade Database's cabinet/marquee art, the IGN/GameSpot fallback
covers, and the Internet Archive's PDF instruction manuals. Every one is a fresh
cross-origin round-trip that keeps a skeleton shimmering until the bytes land,
and it repeats for every visitor on every device.

This module turns each external asset URL into a local read. The first request
for a URL fetches it, stores the bytes under the cache dir on the PVC and serves
them; every request after that — from any browser — comes off the volume. Keyed
by the sha256 of the source URL, and bounded: once the cache grows past `max_mb`
it evicts the least-recently-served files, so it lives comfortably inside the
shared PVC without ever filling it.

One class, one policy per instance: images get an `AssetCache` that accepts
`image/*` under a small cap, manuals get their own that accepts `application/pdf`
under a larger one. Nothing else is ever stored.

SSRF guard: only http(s) URLs whose host resolves to a *public* address are
fetched, and only bytes that sniff to an allowed type under the per-item cap are
stored. The proxy cannot be steered at anything inside the cluster or made to
cache something that isn't the asset it advertises.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger("assetcache")

_UA = "gamedex/1.0 (personal game collection; +https://github.com/zdiemer)"
_TIMEOUT = (5, 30)                  # (connect, read) seconds — PDFs read slower than covers


def _sniff(data: bytes) -> str:
    """Media type from the leading bytes. We trust the file, not the server's
    Content-Type (some mislabel), and this doubles as the is-it-the-right-thing
    gate: anything that doesn't sniff to a type we allow is refused, which keeps
    HTML error pages and JSON out of the cache."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and (b"avif" in data[8:20] or b"heic" in data[8:20]):
        return "image/avif"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    head = data[:256].lstrip()
    if head[:5] == b"<?xml" or head[:4] == b"<svg":
        return "image/svg+xml"
    return "application/octet-stream"


# Ready-made type sets for the two instances the app creates.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/svg+xml"}
PDF_TYPES = {"application/pdf"}


class AssetCache:
    def __init__(self, cache_dir, *, allowed_types: set[str], max_mb: int = 500,
                 max_item_mb: int = 20, enabled: bool = True):
        self.enabled = enabled
        self._dir = Path(cache_dir)
        self._allowed = set(allowed_types)
        self._max = max(0, int(max_mb)) * 1024 * 1024
        self._max_item = max(1, int(max_item_mb)) * 1024 * 1024
        # Locks are keyed by the 2-char shard, not the full hash: 256 buckets is
        # enough to stop two requests fetching the same new asset twice, and it
        # bounds the dict instead of leaking one lock per URL ever seen.
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._added = 0                    # bytes written since the last eviction sweep
        self._last_evict = 0.0
        if self.enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                log.warning("assetcache: cannot create %s (%s) — caching disabled", self._dir, e)
                self.enabled = False

    # ---- public -----------------------------------------------------------
    def ok_url(self, url: str) -> bool:
        """A public http(s) URL we're willing to fetch. Rejects anything that
        resolves to a private / loopback / link-local / reserved address so the
        proxy can't be pointed back into the cluster."""
        try:
            p = urlsplit(url)
        except Exception:
            return False
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        try:
            port = p.port or (443 if p.scheme == "https" else 80)
            infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        return True

    def get(self, url: str):
        """(bytes, media_type) for `url`, fetching and caching on the first miss.

        Returns None when caching is off, the URL is unsafe, or the fetch fails —
        the caller then 302s to the original so the asset still loads. Caching is an
        optimisation, never a gate."""
        if not self.enabled or not self.ok_url(url):
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        path = self._dir / key[:2] / key

        cached = self._read(path)
        if cached is not None:
            return cached, _sniff(cached)

        with self._lock(key[:2]):
            # Another request may have fetched it while we waited on the lock.
            cached = self._read(path)
            if cached is not None:
                return cached, _sniff(cached)
            data = self._fetch(url)
            if data is None:
                return None
            self._store(path, data)
            return data, _sniff(data)

    # ---- internals --------------------------------------------------------
    def _read(self, path: Path):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except Exception as e:
            log.debug("assetcache read %s: %s", path.name, e)
            return None
        # Bump atime for the LRU without rewriting the payload (relatime may not
        # advance it on its own). Metadata-only; best-effort.
        try:
            os.utime(path, (time.time(), path.stat().st_mtime))
        except Exception:
            pass
        return data

    def _fetch(self, url: str):
        try:
            r = requests.get(url, timeout=_TIMEOUT, stream=True,
                             headers={"User-Agent": _UA})
        except Exception as e:
            log.debug("assetcache fetch %s: %s", url, e)
            return None
        try:
            if r.status_code != 200:
                return None
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > self._max_item:
                return None
            buf = bytearray()
            for chunk in r.iter_content(64 * 1024):
                buf.extend(chunk)
                if len(buf) > self._max_item:
                    return None            # runaway body — drop it
        except Exception as e:
            log.debug("assetcache read-body %s: %s", url, e)
            return None
        finally:
            r.close()
        data = bytes(buf)
        if _sniff(data) not in self._allowed:
            return None                    # wrong type (HTML error page, JSON, a PDF where we wanted an image, …)
        return data

    def _store(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)              # atomic: a reader never sees a half-written file
        except Exception as e:
            log.debug("assetcache store %s: %s", path.name, e)
            return
        with self._guard:
            self._added += len(data)
        self._maybe_evict()

    def _maybe_evict(self) -> None:
        if self._max <= 0:
            return
        now = time.monotonic()
        with self._guard:
            # A full walk of the cache dir isn't free, so sweep at most once a
            # minute and only after enough new bytes to plausibly cross the cap.
            if now - self._last_evict < 60 and self._added < self._max // 10:
                return
            self._last_evict = now
            self._added = 0
        self._evict()

    def _evict(self) -> None:
        try:
            files, total = [], 0
            for f in self._dir.rglob("*"):
                if f.suffix == ".tmp" or not f.is_file():
                    continue
                st = f.stat()
                files.append((st.st_atime, st.st_size, f))
                total += st.st_size
            if total <= self._max:
                return
            # Drop least-recently-served first, down to 90% of the cap so we don't
            # re-sweep on every subsequent write.
            target = int(self._max * 0.9)
            files.sort()                   # oldest atime first
            freed = 0
            for _atime, size, f in files:
                if total - freed <= target:
                    break
                try:
                    f.unlink()
                    freed += size
                except Exception:
                    pass
            if freed:
                log.info("assetcache: evicted %d bytes from %s (was %d, cap %d)",
                         freed, self._dir.name, total, self._max)
        except Exception as e:
            log.debug("assetcache evict: %s", e)

    def _lock(self, shard: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(shard, threading.Lock())
