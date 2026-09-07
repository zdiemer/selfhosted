"""Settings, sourced entirely from the chart.

Nothing here has a value the browser can influence. That is the point: the
Deployment's env block is the reviewable statement of what a VM in this
namespace may be, and templates/_helpers.tpl is where it is written.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        if raw:
            logger.warning("%s=%r is not an integer, using %d", name, raw, default)
        return default


def _json(name: str, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON, using default", name)
        return default


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    namespace: str = os.environ.get("VMLAB_NAMESPACE", "vmlab")
    log_level: str = os.environ.get("VMLAB_LOG_LEVEL", "INFO")

    seed_configmap: str = os.environ.get("VMLAB_SEED_CONFIGMAP", "vmlab-catalog-seed")
    user_configmap: str = os.environ.get("VMLAB_USER_CONFIGMAP", "vmlab-catalog-user")

    iso_pvc: str = os.environ.get("VMLAB_ISO_PVC", "vmlab-isos")
    isos_enabled: bool = _bool("VMLAB_ISOS_ENABLED", True)
    fetch_image: str = os.environ.get("VMLAB_FETCH_IMAGE", "alpine:3.22")
    fetch_timeout_seconds: int = _int("VMLAB_FETCH_TIMEOUT_SECONDS", 3600)
    fetch_resources: dict = field(
        default_factory=lambda: _json(
            "VMLAB_FETCH_RESOURCES",
            {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
        )
    )

    vm_service_account: str = os.environ.get("VMLAB_VM_SERVICE_ACCOUNT", "vmlab-vm")
    vm_image: str = os.environ.get("VMLAB_VM_IMAGE", "qemux/qemu")
    vm_image_arm: str = os.environ.get("VMLAB_VM_IMAGE_ARM", "qemux/qemu-arm")
    vm_tag: str = os.environ.get("VMLAB_VM_TAG", "7.50")
    vm_pull_policy: str = os.environ.get("VMLAB_VM_PULL_POLICY", "IfNotPresent")

    default_ttl_seconds: int = _int("VMLAB_DEFAULT_TTL_SECONDS", 14400)
    max_ttl_seconds: int = _int("VMLAB_MAX_TTL_SECONDS", 86400)
    idle_timeout_seconds: int = _int("VMLAB_IDLE_TIMEOUT_SECONDS", 1800)
    # How long a crashed guest's pod is kept so its log can still be read. It
    # still holds pod quota and its disk, so this is a debugging window rather
    # than an archive.
    terminal_grace_seconds: int = _int("VMLAB_TERMINAL_GRACE_SECONDS", 1800)
    # How often running guests are re-photographed for the catalog. Every
    # pass opens a VNC connection per guest, so this trades freshness against
    # noise on the console the user may be actively using.
    live_thumbnail_seconds: int = _int("VMLAB_LIVE_THUMBNAIL_SECONDS", 30)

    max_cores: int = _int("VMLAB_MAX_CORES", 8)
    max_memory_mib: int = _int("VMLAB_MAX_MEMORY_MIB", 8192)
    max_disk_gib: int = _int("VMLAB_MAX_DISK_GIB", 128)
    max_concurrent_vms: int = _int("VMLAB_MAX_CONCURRENT_VMS", 6)

    persist_storage_class: str = os.environ.get("VMLAB_PERSIST_STORAGE_CLASS", "local-path")

    cpu_request: str = os.environ.get("VMLAB_CPU_REQUEST", "500m")
    overhead_memory_mib: int = _int("VMLAB_OVERHEAD_MEMORY_MIB", 512)

    # Mirrors values.yaml. The fallback matters because a profile that is
    # missing here cannot be launched at all — build_session_pod refuses rather
    # than defaulting, since an unlabelled pod is an unpoliced one. Note which
    # direction the fallback errs in: it reproduces the *restrictions*
    # (NETWORK=N and an empty capability set for `none`), never a permission.
    network_profiles: dict = field(
        default_factory=lambda: _json(
            "VMLAB_NETWORK_PROFILES",
            {
                "none": {"qemuNetwork": "N", "capabilities": [], "policy": "deny"},
                "internet": {
                    "qemuNetwork": "slirp",
                    "capabilities": ["SETGID", "SETUID"],
                    "policy": "internet",
                },
                "full": {
                    "qemuNetwork": "slirp",
                    "capabilities": ["SETGID", "SETUID"],
                    "policy": "none",
                },
            },
        )
    )
    # NOT defaulted to []. If this env var were ever unset, misspelled or
    # unparseable, an empty default would silently drop the node constraint
    # altogether and let guests schedule onto zachd-ubuntu-laptop-2 — which
    # advertises kvm=1k, carries no taint, has ~2.9Gi free, and sleeps. The
    # fallback is therefore the guard itself, so a config mistake degrades to
    # the safe behaviour rather than away from it.
    exclude_node_labels: list = field(
        default_factory=lambda: _json(
            "VMLAB_EXCLUDE_NODE_LABELS", [{"key": "device-type", "values": ["laptop"]}]
        )
    )
    prefer_nodes: list = field(default_factory=lambda: _json("VMLAB_PREFER_NODES", []))

    # Where a staged ISO is mounted. findBootFile() in the image's install.sh
    # scans / at maxdepth 1 for boot.{iso,img,raw,qcow2} and short-circuits
    # before BOOT is read, which is the only reason the cache is authoritative
    # over the image's baked-in `ENV BOOT="alpine"`.
    boot_iso_path: str = "/boot.iso"


settings = Settings()
