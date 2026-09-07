"""Save-point metadata and screenshots, on a volume session pods never mount.

WHERE THE TRUTH LIVES. The save point itself is an internal qcow2 snapshot on
the guest's own disk — QEMU owns it, and `qmp.list_snapshots` is authoritative
whenever the VM is running. This module stores only what qcow2 cannot: a
screenshot, and a record that survives the VM being stopped (which is exactly
when you want to look at the picture and decide what to load).

So the two are reconciled rather than trusted: a running VM's list comes from
QEMU and this cache is corrected to match; a stopped VM's list comes from here,
clearly marked as remembered rather than observed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Same discipline as every other caller-supplied string here. This one is
# stricter than it looks like it needs to be, because it reaches TWO dangerous
# places: a QEMU HMP command line, and `-loadvm <name>` in ARGUMENTS, which
# qemux/qemu expands UNQUOTED into the qemu argv.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

MAX_PER_CONFIG = 12


class SnapshotError(ValueError):
    pass


def validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise SnapshotError(
            "save point names must be 1-31 characters of lowercase letters, "
            "digits, hyphen or underscore"
        )
    return name


def _root() -> str:
    return os.environ.get("VMLAB_SNAPSHOT_MOUNT", "/snapshots")


def enabled() -> bool:
    return os.path.isdir(_root())


def _dir(slug: str) -> str:
    return os.path.join(_root(), slug)


def _manifest_path(slug: str) -> str:
    return os.path.join(_dir(slug), "manifest.json")


def screenshot_path(slug: str, name: str, *, full: bool = False) -> str:
    suffix = ".full.png" if full else ".png"
    return os.path.join(_dir(slug), f"{name}{suffix}")


def _read_manifest(slug: str) -> dict:
    try:
        with open(_manifest_path(slug), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(slug: str, data: dict) -> None:
    os.makedirs(_dir(slug), exist_ok=True)
    # Written to a temp file and renamed: the UI reads this on every catalog
    # poll, and a half-written manifest would surface as "no save points" —
    # i.e. as data loss, on a page whose whole job is telling you what you have.
    fd, tmp = tempfile.mkstemp(dir=_dir(slug), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _manifest_path(slug))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".w-", suffix=".tmp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def record(
    slug: str,
    name: str,
    *,
    png: bytes | None,
    full_png: bytes | None = None,
    width: int = 0,
    height: int = 0,
    boot_from_iso: bool = False,
    hardware: str = "",
    hardware_fields: dict | None = None,
) -> dict:
    entry = {
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "width": width,
        "height": height,
        "hasScreenshot": bool(png),
        # Restoring needs the same device topology as the save. Without this a
        # save point taken from a live CD cannot be loaded at all.
        "bootFromIso": bool(boot_from_iso),
        # The machine this was taken on. -loadvm restores device state onto
        # whatever QEMU builds NOW, so a config edited since is not loadable.
        "hardware": hardware,
        "hardwareFields": hardware_fields or {},
    }
    if png:
        _atomic_write(screenshot_path(slug, name), png)
        if full_png:
            _atomic_write(screenshot_path(slug, name, full=True), full_png)
    data = _read_manifest(slug)
    data[name] = entry
    _write_manifest(slug, data)
    return entry


def forget(slug: str, name: str) -> None:
    data = _read_manifest(slug)
    data.pop(name, None)
    _write_manifest(slug, data)
    for full in (False, True):
        try:
            os.unlink(screenshot_path(slug, name, full=full))
        except OSError:
            pass


def listing(slug: str, *, live: list[dict] | None = None) -> list[dict]:
    """Merge what QEMU holds with what was remembered.

    `live` is None when the VM is not running, in which case every entry is
    reported as remembered. When it IS running, QEMU wins: a save point deleted
    outside this UI disappears here too, and one created outside it still shows
    up (without a screenshot, honestly labelled).
    """
    remembered = _read_manifest(slug)

    if live is None:
        return sorted(
            (dict(e, observed=False) for e in remembered.values()),
            key=lambda e: e.get("created", ""),
            reverse=True,
        )

    live_names = {s["name"] for s in live}
    out = []
    for snap in live:
        entry = dict(remembered.get(snap["name"], {"name": snap["name"], "created": ""}))
        entry.update(observed=True, vmSize=snap.get("vmSize", ""))
        entry.setdefault("hasScreenshot", False)
        out.append(entry)

    # Drop cache rows QEMU no longer has, so a stale screenshot cannot offer a
    # save point that would fail to load.
    stale = set(remembered) - live_names
    if stale:
        for name in stale:
            forget(slug, name)
        logger.info("dropped %d stale save point(s) for %s", len(stale), slug)

    return sorted(out, key=lambda e: e.get("created", ""), reverse=True)


def count(slug: str) -> int:
    return len(_read_manifest(slug))


# ---------------------------------------------------------------------------
# Live tiles
# ---------------------------------------------------------------------------


def live_path(slug: str) -> str:
    """Kept apart from the save points, under a name no save point can take:
    NAME_RE forbids a leading dot, so `.live` cannot collide with one."""
    return os.path.join(_dir(slug), ".live.png")


def write_live(slug: str, png: bytes) -> None:
    _atomic_write(live_path(slug), png)
