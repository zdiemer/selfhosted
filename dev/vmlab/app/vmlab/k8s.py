"""Kubernetes calls and the catalog store.

Every function here is SYNCHRONOUS, because the official client is. Routes that
call into this module are declared `def`, not `async def`, so Starlette runs
them in its threadpool — a blocking call inside an async route would stall the
event loop and, with it, the console proxy for every other session.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config as kube_config
from kubernetes.client.rest import ApiException

from vmlab import qmp, screenshot, snapshots, spec
from vmlab.config import settings

logger = logging.getLogger(__name__)

_core: client.CoreV1Api | None = None
_batch: client.BatchV1Api | None = None

RELEASE = "vmlab"
SESSION_SELECTOR = "app.kubernetes.io/name=vmlab,app.kubernetes.io/component=session"

# The annotation the reaper reads. Bumped whenever a console websocket is open,
# so "idle" means "nobody is looking at it", not "nothing is running" — a guest
# compiling something with the tab open must not be culled.
LAST_SEEN = "vmlab.zachd/last-seen"

# Where the UI mounts the ISO cache, read-only. See iso_present().
ISO_MOUNT = os.environ.get("VMLAB_ISO_MOUNT", "/isos")


def init() -> None:
    global _core, _batch
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config()
    _core = client.CoreV1Api()
    _batch = client.BatchV1Api()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Catalog: two ConfigMaps, merged on read
# ---------------------------------------------------------------------------


def _read_configmap(name: str) -> dict[str, str]:
    try:
        cm = _core.read_namespaced_config_map(name, settings.namespace)
        return cm.data or {}
    except ApiException as exc:
        if exc.status == 404:
            return {}
        raise


def _seed_configs() -> list[dict]:
    data = _read_configmap(settings.seed_configmap)
    try:
        return json.loads(data.get("catalog.json", "[]"))
    except json.JSONDecodeError:
        logger.exception("seed catalog is not valid JSON; treating as empty")
        return []


def _user_configs() -> list[dict]:
    data = _read_configmap(settings.user_configmap)
    try:
        return json.loads(data.get("catalog.json", "[]"))
    except json.JSONDecodeError:
        logger.exception("user catalog is not valid JSON; treating as empty")
        return []


def _normalise(cfg: dict, source: str) -> dict:
    """Put every config through the same validation, wherever it came from.

    Seed entries used to be passed through raw, which was wrong in two ways
    that only showed up once values.yaml gained an entry omitting a field:
    build_session_pod does config["bootMode"] and raised KeyError at LAUNCH
    rather than at load, and — worse — none of validate_config's DEFAULTING
    applied, so a save-point guest declared without a bootMode would never have
    received the BIOS default that makes savevm work at all.

    A seed entry that cannot be validated is marked broken rather than raising,
    so one bad line in values.yaml disables one tile instead of 500-ing the
    whole catalog.
    """
    try:
        return {**spec.validate_config(cfg), "source": source}
    except spec.ValidationError as exc:
        return {
            **cfg,
            "source": source,
            "invalid": str(exc),
            "slug": cfg.get("slug") or "?",
            "name": cfg.get("name") or cfg.get("slug") or "?",
        }


def list_configs() -> list[dict]:
    """Seed entries first, then user entries. A user entry never shadows a seed
    entry — create_config refuses a duplicate slug — so the merge needs no
    precedence rule, which is one less thing to get wrong."""
    out = []
    for cfg in _seed_configs():
        out.append(_normalise(cfg, "seed"))
    seen = {c["slug"] for c in out}
    for cfg in _user_configs():
        if cfg.get("slug") in seen:
            continue
        out.append(_normalise(cfg, "user"))
    return out


def get_config(slug: str) -> dict | None:
    for cfg in list_configs():
        if cfg.get("slug") == slug:
            return cfg
    return None


def _write_user_configs(configs: list[dict]) -> None:
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": settings.user_configmap,
            "labels": {"app.kubernetes.io/name": "vmlab", "app.kubernetes.io/instance": RELEASE},
            # Helm does not own this object and must not adopt it. The keep
            # policy is belt and braces for the same reason the two ConfigMaps
            # are separate at all: a helm upgrade must never be able to delete
            # configs somebody added from the browser.
            "annotations": {"helm.sh/resource-policy": "keep"},
        },
        "data": {"catalog.json": json.dumps(configs, indent=2)},
    }
    try:
        _core.replace_namespaced_config_map(settings.user_configmap, settings.namespace, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        _core.create_namespaced_config_map(settings.namespace, body)


def create_config(raw: dict) -> dict:
    existing = {c["slug"] for c in list_configs()}
    cfg = spec.validate_config(raw, existing_slugs=existing)
    _write_user_configs(_user_configs() + [cfg])
    logger.info("created config %s", cfg["slug"])
    return cfg


def delete_config(slug: str) -> bool:
    """Only user configs. A seed entry is Helm's and would reappear on the next
    upgrade looking like the delete silently failed, so it is refused outright
    instead."""
    remaining = [c for c in _user_configs() if c.get("slug") != slug]
    if len(remaining) == len(_user_configs()):
        return False
    _write_user_configs(remaining)
    logger.info("deleted config %s", slug)
    return True


# ---------------------------------------------------------------------------
# ISO cache
# ---------------------------------------------------------------------------


def iso_present(slug: str) -> bool:
    """Whether the ISO is actually on the cache volume.

    This reads the directory, and an earlier version deliberately did not —
    it inferred "cached" from a succeeded fetch Job instead, to avoid mounting
    the shared volume into the UI pod. That reasoning was wrong twice over:

      - The cache is NOT guest-writable. Session pods never mount it at all;
        an initContainer copies out of it and the VM container only ever sees
        its own private copy. Reading it read-only here adds no exposure.
      - Jobs are garbage collected. ttlSecondsAfterFinished is 600, so the
        "cached" signal reliably vanished ten minutes after every fetch and
        every ISO reverted to "absent" — which then made launch() refuse to
        start anything. The lab broke itself on a timer.

    Reading the volume is also what makes hand-staged media work: an ISO copied
    in with kubectl cp now simply appears, with no fake Job to go with it.
    """
    # Globbed by extension rather than looked up from the config: a config
    # read would put an API call behind every catalog poll, and would make this
    # untestable without a cluster. The slug is SLUG_RE-validated, so the
    # pattern cannot escape the directory.
    for media in sorted(spec.BOOT_MEDIA):
        path = os.path.join(ISO_MOUNT, spec.iso_filename(slug, media))
        try:
            if os.path.getsize(path) > 0:
                return True
        except OSError:
            continue
    return False


def iso_status(slug: str) -> str:
    if iso_present(slug):
        return "cached"
    # Not on disk, so the only question left is whether something is on its way.
    # THAT is what Jobs are good for, and their GC is harmless here.
    jobs = _batch.list_namespaced_job(
        settings.namespace, label_selector=f"vmlab.zachd/role=iso-fetch,vmlab.zachd/config={slug}"
    )
    if not jobs.items:
        return "absent"
    for job in jobs.items:
        if (job.status.active or 0) > 0:
            return "fetching"
    for job in jobs.items:
        if (job.status.failed or 0) > 0:
            return "failed"
    return "absent"


def start_fetch(slug: str) -> str:
    cfg = get_config(slug)
    if cfg is None:
        raise spec.ValidationError(f"no such config: {slug}")
    # Two fetches for one slug write the same .part file and interleave into
    # garbage. The UI hides the button while a fetch runs, but that is a
    # rendering detail: a double-click, a stale tab or a direct API call all
    # reach here. Refuse rather than race.
    if iso_status(slug) == "fetching":
        raise spec.ValidationError(f"a download for {slug} is already running")
    job_name = f"{RELEASE}-fetch-{slug}-{secrets.token_hex(3)}"[:63]
    body = spec.build_fetch_job(config=cfg, job_name=job_name, release=RELEASE)
    _batch.create_namespaced_job(settings.namespace, body)
    logger.info("fetching ISO for %s as %s", slug, job_name)
    return job_name


def clear_failed_fetches(slug: str) -> None:
    """Drop terminal Jobs so a retry is not mistaken for the old failure.

    propagation_policy is Background rather than the default: deleting a Job
    without it can leave its pod running, and an orphaned fetch pod keeps
    writing the very .part file the next attempt is about to use.
    """
    jobs = _batch.list_namespaced_job(
        settings.namespace, label_selector=f"vmlab.zachd/role=iso-fetch,vmlab.zachd/config={slug}"
    )
    for job in jobs.items:
        if (job.status.active or 0) == 0:
            try:
                _batch.delete_namespaced_job(
                    job.metadata.name, settings.namespace, propagation_policy="Background"
                )
            except ApiException:
                logger.warning("could not delete job %s", job.metadata.name)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _session_view(pod) -> dict:
    ready = False
    for cond in pod.status.conditions or []:
        if cond.type == "Ready" and cond.status == "True":
            ready = True
    return {
        "id": pod.metadata.name,
        "slug": (pod.metadata.labels or {}).get("vmlab.zachd/config", ""),
        "name": (pod.metadata.annotations or {}).get("vmlab.zachd/display-name", ""),
        "network": (pod.metadata.labels or {}).get("vmlab.zachd/network", ""),
        "phase": pod.status.phase,
        "ready": ready,
        "podIP": pod.status.pod_ip,
        "node": pod.spec.node_name,
        "startedAt": pod.status.start_time.isoformat() if pod.status.start_time else None,
        "bootFromIso": (pod.metadata.annotations or {}).get(
            "vmlab.zachd/boot-from-iso"
        ) == "true",
    }


def list_sessions() -> list[dict]:
    pods = _core.list_namespaced_pod(settings.namespace, label_selector=SESSION_SELECTOR)
    return [_session_view(p) for p in pods.items if p.metadata.deletion_timestamp is None]


def get_session(session_id: str) -> dict | None:
    if not spec.SESSION_RE.match(session_id or ""):
        return None
    try:
        pod = _core.read_namespaced_pod(session_id, settings.namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    labels = pod.metadata.labels or {}
    # Name-based lookup could otherwise reach any pod in the namespace, so the
    # label is checked rather than assumed. Without this, /vm/<anything>/ would
    # proxy to arbitrary pods — including this one.
    if labels.get("app.kubernetes.io/component") != "session":
        return None
    return _session_view(pod)


def _disk_exists(slug: str) -> bool:
    name = spec.disk_pvc_name(RELEASE, slug)
    try:
        _core.read_namespaced_persistent_volume_claim(name, settings.namespace)
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise


def launch(
    slug: str,
    *,
    boot_from_iso: bool | None = None,
    ttl_seconds: int | None = None,
    load_snapshot: str | None = None,
) -> dict:
    cfg = get_config(slug)
    if cfg is None:
        raise spec.ValidationError(f"no such config: {slug}")

    if cfg.get("invalid"):
        raise spec.ValidationError(f"{slug} is misconfigured: {cfg['invalid']}")

    running = [s for s in list_sessions() if s["phase"] in {"Pending", "Running"}]
    if len(running) >= settings.max_concurrent_vms:
        raise spec.ValidationError(
            f"{len(running)} of {settings.max_concurrent_vms} VM slots are in use — "
            "stop one first"
        )
    if any(s["slug"] == slug for s in running):
        raise spec.ValidationError(f"{slug} is already running")

    fresh_disk = False
    if cfg.get("persist"):
        if not _disk_exists(slug):
            _core.create_namespaced_persistent_volume_claim(
                settings.namespace,
                spec.build_disk_pvc(
                    config=cfg,
                    release=RELEASE,
                    storage_class=settings.persist_storage_class,
                ),
            )
            fresh_disk = True
            logger.info("created persistent disk for %s", slug)

    if load_snapshot:
        if not cfg.get("savepoints"):
            raise spec.ValidationError(f"{slug} does not use save points")
        # -loadvm restores VM state onto whatever devices QEMU has NOW, so the
        # topology must match the moment of the save. Forcing the ISO off here
        # (the first attempt) made a live-CD save point unrestorable:
        # "Device 'boot' is writable but does not support snapshots".
        remembered = {e["name"]: e for e in snapshots.listing(slug)}
        entry = remembered.get(load_snapshot)
        if entry is None:
            raise spec.ValidationError(f"no save point named {load_snapshot!r}")
        boot_from_iso = bool(entry.get("bootFromIso", False))

    if boot_from_iso is None:
        # For an ephemeral config there is nothing but the ISO to boot. For a
        # persisted one, attach the installer only while the disk is new —
        # findBootFile() short-circuits BEFORE the data-disk check, so leaving
        # the ISO attached would re-run the installer on every launch forever.
        boot_from_iso = True if not cfg.get("persist") else fresh_disk

    if boot_from_iso and not iso_present(slug):
        raise spec.ValidationError(
            f"the ISO for {slug} is not cached yet — fetch it first"
        )

    session_id = f"vm-{slug}-{secrets.token_hex(3)}"[:63]
    pod = spec.build_session_pod(
        config=cfg,
        session_id=session_id,
        release=RELEASE,
        boot_from_iso=boot_from_iso,
        ttl_seconds=ttl_seconds or settings.default_ttl_seconds,
        load_snapshot=load_snapshot,
    )
    pod["metadata"].setdefault("annotations", {})[LAST_SEEN] = _now()
    _core.create_namespaced_pod(settings.namespace, pod)
    logger.info(
        "launched %s (%s, boot_from_iso=%s, loadvm=%s)",
        session_id, slug, boot_from_iso, load_snapshot or "-",
    )
    return {
        "id": session_id,
        "slug": slug,
        "bootFromIso": boot_from_iso,
        "loadedSnapshot": load_snapshot,
    }


def stop(session_id: str) -> bool:
    if get_session(session_id) is None:
        return False
    try:
        _core.delete_namespaced_pod(
            session_id, settings.namespace, grace_period_seconds=5
        )
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
    logger.info("stopped %s", session_id)
    return True


def touch(session_id: str) -> None:
    """Record that somebody is watching. Best-effort: losing this races a
    console into being reaped early, which is annoying, but failing the
    websocket over it would be worse."""
    try:
        _core.patch_namespaced_pod(
            session_id,
            settings.namespace,
            {"metadata": {"annotations": {LAST_SEEN: _now()}}},
        )
    except ApiException:
        logger.debug("could not touch %s", session_id, exc_info=True)


def session_log(session_id: str, tail: int = 200) -> str:
    if get_session(session_id) is None:
        return ""
    try:
        return _core.read_namespaced_pod_log(
            session_id, settings.namespace, container="qemu", tail_lines=tail
        )
    except ApiException as exc:
        return f"(log unavailable: {exc.status})"


def reap_terminal() -> int:
    """Remove session pods that have finished, successfully or not.

    activeDeadlineSeconds and the idle reaper both handle RUNNING guests; a pod
    that has already exited is invisible to both and simply accumulates. That
    is not merely untidy: a Failed pod still counts against the namespace pod
    quota and still holds its per-config disk, so a guest that crashed once
    silently blocks the next launch of the same config from getting a fresh
    disk. Observed exactly that.
    """
    culled = 0
    for pod in _core.list_namespaced_pod(
        settings.namespace, label_selector=SESSION_SELECTOR
    ).items:
        if pod.metadata.deletion_timestamp is not None:
            continue
        if pod.status.phase not in {"Failed", "Succeeded"}:
            continue
        # NOT reaped immediately, and this was learned the hard way: an earlier
        # version deleted terminal pods on sight, which meant a guest that
        # failed to boot vanished along with the only log explaining why. The
        # pod holds quota and its disk, so it cannot linger forever — but a
        # failure has to outlive the person noticing it.
        finished = pod.status.start_time
        if finished is not None:
            age = (datetime.now(timezone.utc) - finished).total_seconds()
            if age < settings.terminal_grace_seconds:
                continue
        if True:
            logger.info("reaping %s session %s", pod.status.phase.lower(), pod.metadata.name)
            try:
                _core.delete_namespaced_pod(
                    pod.metadata.name, settings.namespace, grace_period_seconds=0
                )
                culled += 1
            except ApiException as exc:
                if exc.status != 404:
                    logger.warning("could not reap %s: %s", pod.metadata.name, exc)
    return culled


def reap_idle() -> int:
    """Cull sessions nobody has watched for idle_timeout_seconds.

    The hard TTL is activeDeadlineSeconds and belongs to the kubelet, so it
    holds even when this loop does not run. This is only the softer "you walked
    away" cull, which is why it is safe for it to be best-effort.
    """
    cutoff = time.time() - settings.idle_timeout_seconds
    culled = 0
    for session in list_sessions():
        try:
            pod = _core.read_namespaced_pod(session["id"], settings.namespace)
        except ApiException:
            continue
        raw = (pod.metadata.annotations or {}).get(LAST_SEEN)
        if not raw:
            continue
        try:
            seen = datetime.fromisoformat(raw).timestamp()
        except ValueError:
            continue
        if seen < cutoff:
            logger.info("reaping idle session %s", session["id"])
            if stop(session["id"]):
                culled += 1
    return culled


# ---------------------------------------------------------------------------
# Save points
# ---------------------------------------------------------------------------


def _running_session(slug: str) -> dict | None:
    for session in list_sessions():
        if session["slug"] == slug and session["phase"] == "Running" and session.get("podIP"):
            return session
    return None


def list_savepoints(slug: str) -> list[dict]:
    """QEMU is authoritative when the VM is up; the manifest fills in when not.

    A save point that exists only in the manifest would offer a Load button
    that fails at boot, so listing() reconciles rather than merging.
    """
    cfg = get_config(slug)
    if cfg is None or not cfg.get("savepoints") or not snapshots.enabled():
        return []
    session = _running_session(slug)
    live = None
    if session:
        try:
            live = qmp.list_snapshots(session["podIP"])
        except qmp.QmpError:
            # Booting, or resuming, or mid-save. Falling back to the manifest
            # beats showing an empty list and implying the save points are gone.
            logger.debug("monitor not ready for %s", slug, exc_info=True)
    return snapshots.listing(slug, live=live)


def create_savepoint(slug: str, name: str) -> dict:
    cfg = get_config(slug)
    if cfg is None:
        raise spec.ValidationError(f"no such config: {slug}")
    if not cfg.get("savepoints"):
        raise spec.ValidationError(f"{slug} does not use save points")
    name = snapshots.validate_name(name)

    session = _running_session(slug)
    if session is None:
        raise spec.ValidationError(f"{slug} is not running")
    if snapshots.count(slug) >= snapshots.MAX_PER_CONFIG and name not in {
        s["name"] for s in snapshots.listing(slug)
    }:
        raise spec.ValidationError(
            f"{slug} already has {snapshots.MAX_PER_CONFIG} save points — delete one first"
        )

    ip = session["podIP"]
    # Screenshot FIRST, while the guest is still running. Taking it after
    # savevm would picture a resumed machine rather than the moment saved, and
    # taking it during the stop risks catching a blanked display.
    thumb = full_png = None
    width = height = 0
    try:
        # One grab, two encodings. The thumbnail is what the catalog renders;
        # the full-size copy is kept for "what exactly was on screen".
        rgb, width, height = screenshot.capture_raw(ip)
        full_png = screenshot._png(width, height, rgb)
        thumb = screenshot.thumbnail(rgb, width, height)
    except screenshot.ScreenshotError:
        # A save point without a picture is still a save point.
        logger.warning("no screenshot for %s/%s", slug, name, exc_info=True)

    try:
        qmp.save(ip, name)
    except qmp.QmpError as exc:
        raise spec.ValidationError(_explain_save_failure(str(exc), slug)) from exc

    entry = snapshots.record(
        slug, name, png=thumb, full_png=full_png, width=width, height=height,
        boot_from_iso=bool(session.get("bootFromIso")),
    )
    logger.info("saved %s/%s", slug, name)
    return entry


def _explain_save_failure(message: str, slug: str) -> str:
    """Translate QEMU's diagnosis into the thing you can actually do about it.

    The common one is not a bug and not fixable at save time: savevm refuses
    while any WRITABLE non-qcow2 device is attached, and disk.sh force-attaches
    a *hybrid* ISO (Alpine, Bazzite, ReactOS — anything with an MBR signature)
    as a writable usb-disk, bypassing MEDIA_TYPE. Plain ISOs (XP, Red Star,
    Hannah Montana, TempleOS) attach read-only and are skipped, which is why
    those can be saved with the installer still inserted.
    """
    lowered = message.lower()
    if "does not support snapshots" in lowered or "not support snapshot" in lowered:
        return (
            f"{message}. The installer ISO is attached and this image is a hybrid "
            f"ISO, which QEMU attaches as a writable disk — save points cannot "
            f"include one. Finish installing, then stop and relaunch {slug} "
            f"without the installer, and saving will work."
        )
    return message


def delete_savepoint(slug: str, name: str) -> None:
    name = snapshots.validate_name(name)
    session = _running_session(slug)
    if session is not None:
        try:
            qmp.delete(session["podIP"], name)
        except qmp.QmpError as exc:
            raise spec.ValidationError(str(exc)) from exc
    # When the VM is down QEMU cannot be asked to forget it, so only the cache
    # is cleared and the qcow2 keeps the snapshot until the VM next runs. Said
    # plainly in the API response rather than pretending it is gone.
    snapshots.forget(slug, name)
    logger.info("deleted save point %s/%s (vm_running=%s)", slug, name, session is not None)


def savepoint_screenshot(slug: str, name: str, *, full: bool = False) -> str | None:
    name = snapshots.validate_name(name)
    path = snapshots.screenshot_path(slug, name, full=full)
    if os.path.exists(path):
        return path
    # Save points taken before thumbnails existed have only the one file, and a
    # missing full-size copy should fall back rather than 404.
    fallback = snapshots.screenshot_path(slug, name, full=not full)
    return fallback if os.path.exists(fallback) else None
