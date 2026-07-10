"""Poll a Dropbox shared link for the Games workbook and keep it in memory.

The link Zach shares is a *folder* link (`/scl/fo/...`); with `dl=1` Dropbox
serves the folder as a zip, so we extract the target workbook by name. A direct
*file* link (`/scl/fi/...`, `/s/...`, or `dl.dropboxusercontent.com`) with
`dl=1` streams the `.xlsx` bytes directly. We don't branch on the URL — both
arrive as zip bytes (an `.xlsx` *is* a zip), so we sniff the content instead:
if it already looks like a workbook we use it as-is, otherwise we treat it as a
folder zip and pull the workbook member out.

Only when the SHA-256 of the workbook bytes changes do we re-parse, so a poll
that finds no edit is nearly free. On any fetch/parse failure we keep serving
the last good dataset and log a warning.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from parse import parse_workbook

log = logging.getLogger("gamedex.poller")


def _force_direct_download(url: str) -> str:
    """Set dl=1 on a Dropbox share URL (replacing any dl=0)."""
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "dl"]
    query.append(("dl", "1"))
    return urlunparse(parts._replace(query=urlencode(query)))


def _looks_like_workbook(zf: zipfile.ZipFile) -> bool:
    names = set(zf.namelist())
    return "xl/workbook.xml" in names or "[Content_Types].xml" in names and any(
        n.startswith("xl/") for n in names
    )


def _extract_workbook(content: bytes, filename: str) -> bytes:
    """Return workbook bytes from a raw download that is either the xlsx itself
    or a zip of a shared folder containing it."""
    if content[:2] != b"PK":
        raise ValueError(
            "download is not a zip/xlsx (got %r...) — check the Dropbox link is a "
            "valid share URL" % content[:16]
        )
    zf = zipfile.ZipFile(io.BytesIO(content))
    if _looks_like_workbook(zf):
        return content  # the download already *is* the workbook

    # Folder zip: pick the target member by name, else the first .xlsx.
    members = [n for n in zf.namelist() if not n.startswith("__MACOSX/") and not n.endswith("/")]
    want = filename.lower()
    exact = [n for n in members if n.rsplit("/", 1)[-1].lower() == want]
    xlsx = exact or [n for n in members if n.lower().endswith(".xlsx")]
    if not xlsx:
        raise ValueError(
            "no .xlsx found in the shared folder zip (members: %s)" % ", ".join(members[:10])
        )
    return zf.read(xlsx[0])


class DataStore:
    """Thread-safe holder for the parsed dataset and its metadata."""

    def __init__(self, url: str, filename: str, interval: int):
        self._url = _force_direct_download(url) if url else ""
        self._filename = filename
        self._interval = max(30, int(interval))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._data: dict | None = None
        self._hash: str | None = None
        self._etag: str | None = None
        self._last_updated: str | None = None
        self._last_error: str | None = None

    # -- snapshot accessors -------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._data is not None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "data": self._data,
                "meta": {
                    "lastUpdated": self._last_updated,
                    "sourceHash": self._hash,
                    "refreshIntervalSeconds": self._interval,
                    "counts": {k: len(v["rows"]) for k, v in (self._data or {}).items()},
                    "lastError": self._last_error,
                },
            }

    # -- refresh loop -------------------------------------------------------
    def refresh_once(self) -> bool:
        """Fetch + (re)parse if content changed. Returns True if data updated."""
        if not self._url:
            raise RuntimeError("DROPBOX_XLSX_URL is not set")
        # Conditional GET: if Dropbox honors the ETag we skip the whole download
        # when nothing changed. Harmless if ignored — the hash check below still
        # guards against a needless re-parse.
        headers = {"If-None-Match": self._etag} if self._etag else {}
        resp = requests.get(self._url, timeout=90, allow_redirects=True, headers=headers)
        if resp.status_code == 304:
            log.info("poll: not modified (304)")
            with self._lock:
                self._last_error = None
            return False
        resp.raise_for_status()
        etag = resp.headers.get("ETag")
        workbook = _extract_workbook(resp.content, self._filename)
        digest = hashlib.sha256(workbook).hexdigest()

        with self._lock:
            unchanged = digest == self._hash and self._data is not None
        if unchanged:
            log.info("poll: workbook unchanged (%s)", digest[:12])
            with self._lock:
                self._etag = etag
                self._last_error = None
            return False

        parsed = parse_workbook(workbook)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            self._data = parsed
            self._hash = digest
            self._etag = etag
            self._last_updated = now
            self._last_error = None
        log.info(
            "poll: loaded workbook %s (%s)",
            digest[:12],
            ", ".join(f"{k}={len(v['rows'])}" for k, v in parsed.items()),
        )
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as exc:  # keep serving last-good data
                log.warning("poll failed: %s", exc)
                with self._lock:
                    self._last_error = str(exc)
            self._stop.wait(self._interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
