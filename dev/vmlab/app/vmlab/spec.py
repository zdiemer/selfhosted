"""Validation, and the server-side construction of a session pod.

THIS FILE IS THE SECURITY BOUNDARY. Read it as such.

A request may contribute exactly six things — a slug, an ISO URL, memory, cores,
disk size and a network profile name — and every one of them is validated
against an allow-list or clamped to a ceiling from the chart. A request never
contributes an image, a command, a securityContext, a serviceAccountName, a
volume, a mount path, a node selector or a label. Those are assembled here from
`settings`, which comes from the Deployment's env block.

That distinction is the whole difference between "this service can create a pod"
and "this service can run any image as any identity", which is the objection
infra/hatch/templates/rbac.yaml raises against exactly this kind of grant.

If you are adding a knob, the question to ask is not "is this value safe" but
"is every value of this type safe", because the caller picks the value.
"""

from __future__ import annotations

import os
import re
from typing import Any

from vmlab.config import settings

# Mirrors savepoints.qmpPort in values.yaml; the Deployment passes it through.
QMP_PORT = int(os.environ.get("VMLAB_QMP_PORT", "4444"))

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
SESSION_RE = re.compile(r"^[a-z0-9-]{1,63}$")

# Allow-lists, not sanitisers. Each of these lands in an environment variable
# that the image feeds to a QEMU command line, so "reject anything unrecognised"
# is the only safe posture; escaping would be a losing game.
BOOT_MODES = {"uefi", "legacy", "secure", "windows", "windows_legacy", "windows_secure"}
DISK_TYPES = {"ide", "sata", "usb", "nvme", "blk", "scsi", "virtio-blk", "virtio-scsi", "auto"}
ARCHES = {"x86", "arm"}
# Chipset. The image defaults to q35, which is a 2009 PCIe chipset: correct for
# anything modern and a non-starter for guests older than it. Windows XP on q35
# bluescreens with STOP 0x000000A5 before Setup even begins.
MACHINES = {"q35", "pc"}
# How removable media is attached, separately from the data disk. The image
# derives this from DISK_TYPE, and both its "ide" and "sata" settings produce an
# ich9-AHCI controller — which is NOT the legacy PIIX IDE at ports 0x1F0/0x170
# that pre-AHCI guests probe for. "auto" is the plain `media=cdrom` form that
# lands on the machine's built-in IDE bus. TempleOS faults into its debugger
# without it.
MEDIA_TYPES = {"auto", "ide", "sata", "usb", "nvme", "scsi", "blk", "virtio-scsi", "virtio-blk"}

MAX_NAME_LEN = 60
MAX_NOTE_LEN = 400
MAX_URL_LEN = 2048


class ValidationError(ValueError):
    """Rejected input. Surfaces as a 400 with this message."""


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _require_int(raw: Any, field: str, low: int, high: int, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a whole number")
    if value < low:
        raise ValidationError(f"{field} must be at least {low}")
    # Over the ceiling is clamped rather than rejected: the ceiling is a
    # property of the cluster, not a mistake by the caller, and the LimitRange
    # would reject the pod anyway. Being told "you got 8Gi" beats a 400.
    return _clamp(value, low, high)


def validate_iso_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if len(url) > MAX_URL_LEN:
        raise ValidationError("ISO URL is too long")
    if not url.startswith(("http://", "https://")):
        raise ValidationError("ISO URL must be http:// or https://")
    # Whitespace in a URL reaches a shell in the fetch Job. The Job passes it as
    # a quoted argv element, but a URL containing whitespace is malformed
    # regardless, so refusing is both safer and more honest than accepting it.
    if any(c.isspace() for c in url):
        raise ValidationError("ISO URL must not contain whitespace")
    return url


def validate_config(raw: dict, *, existing_slugs: set[str] | None = None) -> dict:
    """Normalise one catalog entry. Raises ValidationError on anything unknown."""
    slug = (raw.get("slug") or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise ValidationError(
            "slug must be 1-31 characters of lowercase letters, digits or hyphens"
        )
    if existing_slugs and slug in existing_slugs:
        raise ValidationError(f"a config named {slug!r} already exists")

    name = (raw.get("name") or slug).strip()[:MAX_NAME_LEN]
    note = (raw.get("note") or "").strip()[:MAX_NOTE_LEN]

    network = (raw.get("network") or "none").strip()
    if network not in settings.network_profiles:
        raise ValidationError(
            f"unknown network profile {network!r}; expected one of "
            + ", ".join(sorted(settings.network_profiles))
        )

    wants_savepoints = bool(raw.get("savepoints")) and bool(raw.get("persist"))
    requested_boot_mode = (raw.get("bootMode") or "").strip().lower()
    # Default to the firmware that actually works for what was asked for. An
    # unspecified save-point guest gets BIOS rather than an error about a
    # setting the caller never chose; an EXPLICIT uefi request still fails
    # loudly below, because silently overriding a stated choice would be worse.
    boot_mode = requested_boot_mode or ("legacy" if wants_savepoints else "uefi")
    if boot_mode not in BOOT_MODES:
        raise ValidationError(f"unknown bootMode {boot_mode!r}")

    disk_type = (raw.get("diskType") or "").strip().lower()
    if disk_type and disk_type not in DISK_TYPES:
        raise ValidationError(f"unknown diskType {disk_type!r}")

    arch = (raw.get("arch") or "x86").strip().lower()
    if arch not in ARCHES:
        raise ValidationError(f"unknown arch {arch!r}")

    # UEFI and save points are mutually exclusive, and the reason is structural
    # rather than a missing feature: qemux/qemu backs the UEFI variable store
    # with a WRITABLE raw pflash drive, and savevm refuses whenever any writable
    # non-qcow2 device is attached ("Device 'pflash1' is writable but does not
    # support snapshots"). SeaBIOS has no such device, which is exactly why the
    # legacy guests could be saved and the UEFI ones could not.
    #
    # Refused at validation rather than at save time: discovering this after a
    # 40-minute install would be a genuinely bad experience.
    if wants_savepoints and boot_mode in {"uefi", "secure", "windows_secure"}:
        raise ValidationError(
            f"save points need a BIOS guest: bootMode {boot_mode!r} adds a writable "
            "UEFI variable store that QEMU cannot snapshot. Use 'legacy' "
            "(or 'windows_legacy')."
        )

    machine = (raw.get("machine") or "").strip().lower()
    if machine and machine not in MACHINES:
        raise ValidationError(f"unknown machine {machine!r}")

    media_type = (raw.get("mediaType") or "").strip().lower()
    if media_type and media_type not in MEDIA_TYPES:
        raise ValidationError(f"unknown mediaType {media_type!r}")

    return {
        "slug": slug,
        "name": name,
        "note": note,
        "iso": validate_iso_url(raw.get("iso", "")),
        "sha256": (raw.get("sha256") or "").strip().lower(),
        "memoryMib": _require_int(raw.get("memoryMib"), "memory", 128, settings.max_memory_mib, 1024),
        "cores": _require_int(raw.get("cores"), "cores", 1, settings.max_cores, 1),
        "diskGib": _require_int(raw.get("diskGib"), "disk", 1, settings.max_disk_gib, 8),
        "bootMode": boot_mode,
        "diskType": disk_type,
        "arch": arch,
        "machine": machine,
        "mediaType": media_type,
        "network": network,
        "persist": bool(raw.get("persist", False)),
        # Save points need somewhere for the qcow2 internal snapshot to live,
        # and an emptyDir disappears with the pod — so they imply persistence
        # rather than being independent of it. Stated here so the UI cannot
        # offer a save point that would evaporate on stop.
        "savepoints": bool(raw.get("savepoints", False)) and bool(raw.get("persist", False)),
        # Whether the staged copy may have its MBR signature cleared. Defaults
        # on for save-point guests (it is what lets a hybrid ISO attach
        # read-only, and therefore what lets savevm run with the CD in), but it
        # MODIFIES sector 0 — which breaks any ISO carrying an implanted
        # whole-image checksum. Fedora-family media (Bazzite) run isomd5sum at
        # boot and HALT when it fails, so they must opt out and be installed to
        # disk before their first save point.
        "flattenIso": bool(raw.get("flattenIso", True)),
    }


def _sha256_ok(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def iso_filename(slug: str) -> str:
    # slug is already SLUG_RE-validated, so this cannot traverse.
    return f"{slug}.iso"


def disk_pvc_name(release: str, slug: str) -> str:
    return f"{release}-disk-{slug}"[:63]


# ---------------------------------------------------------------------------
# Pod construction
# ---------------------------------------------------------------------------


def _affinity() -> dict:
    """Node placement, expressed without ever reading a Node.

    Requesting devices.kubevirt.io/kvm already excludes the control-plane
    workstation and the cordoned laptops, because they advertise none. It does
    NOT exclude zachd-ubuntu-laptop-2, which advertises kvm=1k and carries no
    taint — only the label device-type=laptop. It has ~2.9Gi free and it sleeps
    and roams, so it needs an explicit term.

    NotIn also matches nodes where the key is absent, which is what keeps the
    other five KVM nodes eligible.
    """
    required = [
        {"key": rule["key"], "operator": "NotIn", "values": list(rule["values"])}
        for rule in settings.exclude_node_labels
        if rule.get("key") and rule.get("values")
    ]
    affinity: dict[str, Any] = {}
    if required:
        affinity["requiredDuringSchedulingIgnoredDuringExecution"] = {
            "nodeSelectorTerms": [{"matchExpressions": required}]
        }
    if settings.prefer_nodes:
        # Soft: zachd-ubuntu-1 hosts win11 and has ~0.4 CPU free, but it should
        # still be usable once -4 and -5 fill up. A required term here would
        # turn "the lab is busy" into "the lab is broken".
        affinity["preferredDuringSchedulingIgnoredDuringExecution"] = [
            {
                "weight": 100,
                "preference": {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": list(settings.prefer_nodes),
                        }
                    ]
                },
            }
        ]
    # Nested under `affinity`, not spread bare into the pod spec: `spec.affinity
    # .nodeAffinity` is the only place the API server reads it from, and a
    # stray `spec.nodeAffinity` is silently dropped rather than rejected — so
    # the guests would have quietly scheduled anywhere.
    return {"affinity": {"nodeAffinity": affinity}} if affinity else {}


def build_session_pod(
    *,
    config: dict,
    session_id: str,
    release: str,
    boot_from_iso: bool,
    ttl_seconds: int,
    load_snapshot: str | None = None,
) -> dict:
    """Assemble the V1Pod for one session. Every field here is server-chosen."""
    if not SESSION_RE.match(session_id):
        raise ValidationError("invalid session id")

    slug = config["slug"]
    profile = settings.network_profiles.get(config["network"])
    if profile is None:
        # Unreachable via validate_config, but a config could have been written
        # under an older chart that had a profile this one no longer defines.
        # Refusing beats silently downgrading to an unlabelled — and therefore
        # unpoliced — pod.
        raise ValidationError(f"unknown network profile {config['network']!r}")

    ttl = _clamp(int(ttl_seconds), 60, settings.max_ttl_seconds)
    memory_mib = _clamp(int(config["memoryMib"]), 128, settings.max_memory_mib)
    cores = _clamp(int(config["cores"]), 1, settings.max_cores)
    disk_gib = _clamp(int(config["diskGib"]), 1, settings.max_disk_gib)

    savepoints = bool(config.get("savepoints"))
    if load_snapshot is not None:
        if not savepoints:
            raise ValidationError("this config does not use save points")
        # NAME_RE, not a general sanitiser. This string ends up inside
        # ARGUMENTS, which entry.sh expands UNQUOTED into the qemu argv
        # (`exec "${cmd[@]}" ${ARGS:+ $ARGS}`), so a space or a semicolon here
        # would be argument injection into the emulator's command line.
        from vmlab.snapshots import validate_name

        load_snapshot = validate_name(load_snapshot)

    image_base = settings.vm_image_arm if config.get("arch") == "arm" else settings.vm_image
    image = f"{image_base}:{settings.vm_tag}"

    env = [
        {"name": "RAM_SIZE", "value": f"{memory_mib}M"},
        {"name": "CPU_CORES", "value": str(cores)},
        {"name": "DISK_SIZE", "value": f"{disk_gib}G"},
        # Which of the two independent network levers this is: NETWORK=N means
        # QEMU builds the guest no NIC at all, so it is air-gapped regardless of
        # NetworkPolicy. The policy contains the container around it.
        {"name": "NETWORK", "value": profile["qemuNetwork"]},
        {"name": "BOOT_MODE", "value": config["bootMode"]},
    ]
    if config.get("diskType"):
        env.append({"name": "DISK_TYPE", "value": config["diskType"]})
    if config.get("machine"):
        # Allow-listed above, so this cannot become arbitrary -machine text.
        env.append({"name": "MACHINE", "value": config["machine"]})
    if config.get("mediaType"):
        env.append({"name": "MEDIA_TYPE", "value": config["mediaType"]})
    if savepoints:
        # qcow2 is not a preference: savevm stores VM state INSIDE the disk
        # image, and raw has nowhere to put it. Changing this on an existing
        # raw disk would not convert it, so it is fixed per config rather than
        # toggleable at launch.
        env.append({"name": "DISK_FMT", "value": "qcow2"})
        # qemux/qemu wires this straight to `-qmp`, defaulting a bare number to
        # tcp. Reachable only from the vmlab pod — see the ingress policy that
        # now covers every profile including `full`.
        env.append({"name": "QMP", "value": str(QMP_PORT)})
        # savevm serialises VM state, and refuses outright if any device is
        # non-migratable: "State blocked by non-migratable CPU device (invtsc
        # flag)". qemux/qemu adds +invtsc whenever the host TSC is stable —
        # which every node here has — so save points are impossible without
        # turning it back off. CPU_FLAGS is concatenated AFTER the image's own
        # feature list, and QEMU takes the last setting for a flag, so this
        # overrides rather than conflicts.
        #
        # The cost is real but small and confined to save-point guests: the
        # guest loses an invariant TSC and falls back to a slower clocksource.
        # A lab VM would rather resume than keep perfect time.
        env.append({"name": "CPU_FLAGS", "value": "-invtsc"})
        # Second non-migratable blocker, found the same way as the first:
        # "'hv-passthrough' CPU flag prevents migration". qemux/qemu builds its
        # Hyper-V enlightenments on hv_passthrough, which by construction
        # mirrors whatever the host CPU offers and therefore cannot be
        # serialised. HV=N drops the block entirely.
        #
        # Nearly free for the guest that needs save points most: Hyper-V
        # enlightenments target Vista and later, so Windows XP cannot use them
        # at all. A modern Windows guest would lose some paravirtual
        # acceleration, which is the trade for being able to resume it.
        env.append({"name": "HV", "value": "N"})
    if load_snapshot:
        env.append({"name": "ARGUMENTS", "value": f"-loadvm {load_snapshot}"})
    if not boot_from_iso:
        # "none", NOT "". Empty is the one value that does the opposite of what
        # it looks like: install.sh treats it as unset and falls back to
        # `ENV BOOT="alpine"`, downloading Alpine and attaching it as a hybrid
        # (writable) disk. Observed exactly that — a guest meant to resume from
        # its own disk instead fetched Alpine, which then broke -loadvm with
        # "Device 'boot' is writable but does not support snapshots".
        #
        # With "none": if the disk has data, install.sh short-circuits before
        # the URL check and boots it. If the disk is EMPTY it errors out with
        # exit 64 — a loud, accurate failure ("you asked to boot a disk with
        # nothing on it") rather than a silent Alpine download.
        env.append({"name": "BOOT", "value": "none"})

    volumes: list[dict] = [
        # nginx serves the console on :8006 and its error.log is www-data:adm
        # 0640. Root without CAP_DAC_OVERRIDE cannot write it, and the container
        # dies at server.sh line 14 before QEMU ever starts. Shadowing the
        # directory is what lets the capability set stay empty.
        {"name": "nginx-log", "emptyDir": {"sizeLimit": "16Mi"}},
    ]
    mounts: list[dict] = [{"name": "nginx-log", "mountPath": "/var/log/nginx"}]

    if config.get("persist"):
        volumes.append(
            {
                "name": "storage",
                "persistentVolumeClaim": {"claimName": disk_pvc_name(release, slug)},
            }
        )
    else:
        volumes.append(
            {"name": "storage", "emptyDir": {"sizeLimit": f"{disk_gib + 2}Gi"}}
        )
    mounts.append({"name": "storage", "mountPath": "/storage"})

    init_containers: list[dict] = []
    if boot_from_iso:
        # The staged private copy. The VM container mounts THIS, never the
        # shared cache — so a guest has no handle on the ISOs other guests boot
        # from, and cannot corrupt them.
        #
        # A copy rather than a read-only mount because disk.sh force-attaches a
        # hybrid ISO (MBR signature != 0000 — Alpine, Bazzite, most modern
        # Linux) as a WRITABLE usb-storage disk, bypassing MEDIA_TYPE. QEMU then
        # opens it read-write and a read-only mount fails outright.
        #
        # For save-point guests the copy is also FLATTENED: its two-byte MBR
        # signature is zeroed. That is what lets a hybrid ISO be attached as a
        # read-only CD-ROM, and therefore what lets savevm run at all while the
        # ISO is inserted — savevm refuses whenever any writable non-qcow2
        # device is present. Booting is unaffected because a cdrom device boots
        # through El Torito (the ISO9660 boot catalog), not the MBR.
        #
        # Safe precisely because this is a private per-session copy: the shared
        # cache keeps its real bytes, so nothing else ever sees a modified ISO.
        volumes.append({"name": "bootiso", "emptyDir": {"sizeLimit": "16Gi"}})
        volumes.append(
            {
                "name": "isos",
                "persistentVolumeClaim": {"claimName": settings.iso_pvc, "readOnly": True},
            }
        )
        mounts.append(
            {"name": "bootiso", "mountPath": settings.boot_iso_path, "subPath": "boot.iso"}
        )
        init_containers.append(
            {
                "name": "stage-iso",
                "image": settings.fetch_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-c"],
                # The filename is derived from an already-validated slug, and is
                # passed as $1 rather than interpolated into the script body.
                "args": [
                    'set -eu\n'
                    'src="/isos/$1"\n'
                    'flatten="$2"\n'
                    'if [ ! -s "$src" ]; then\n'
                    '  echo "ISO not staged yet: $1 — fetch it first" >&2\n'
                    '  exit 1\n'
                    'fi\n'
                    'cp "$src" /boot/boot.iso\n'
                    # Clear the MBR boot signature on the PRIVATE copy so that
                    # disk.sh stops treating it as a hybrid image. See the
                    # comment on `flatten_hybrid` below for why.
                    'if [ "$flatten" = "yes" ]; then\n'
                    '  sig=$(dd if=/boot/boot.iso bs=1 skip=510 count=2 2>/dev/null | od -An -tx1 | tr -d " \\n")\n'
                    '  if [ "$sig" != "0000" ]; then\n'
                    '    printf "\\0\\0" | dd of=/boot/boot.iso bs=1 seek=510 count=2 conv=notrunc 2>/dev/null\n'
                    '    echo "cleared hybrid MBR signature ($sig) on the private copy"\n'
                    '  fi\n'
                    'fi\n'
                    'ls -lh /boot/boot.iso\n',
                    "sh",
                    iso_filename(slug),
                    "yes" if savepoints and config.get("flattenIso", True) else "no",
                ],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "64Mi"},
                    "limits": {"memory": "128Mi"},
                },
                "volumeMounts": [
                    {"name": "isos", "mountPath": "/isos", "readOnly": True},
                    {"name": "bootiso", "mountPath": "/boot"},
                ],
            }
        )

    # SETGID+SETUID for the internet profile and nothing else: NETWORK=slirp
    # starts dnsmasq, which drops group to `dip`. Measured, not assumed — no
    # NET_ADMIN, no /dev/net/tun, no privileged. The `none` profile needs an
    # entirely empty capability set.
    capabilities: dict[str, Any] = {"drop": ["ALL"]}
    added = [c for c in profile.get("capabilities", []) if c in {"SETGID", "SETUID"}]
    if added:
        capabilities["add"] = added

    resources: dict[str, Any] = {
        "requests": {
            "cpu": settings.cpu_request,
            # The guest's RAM plus the emulator's own footprint. Requesting only
            # a token amount would let the scheduler overcommit a node into
            # swapping, which for a VM host is indistinguishable from a hang.
            "memory": f"{memory_mib + settings.overhead_memory_mib}Mi",
        },
        "limits": {
            "cpu": str(cores),
            "memory": f"{memory_mib + settings.overhead_memory_mib}Mi",
        },
    }
    if config.get("arch") != "arm":
        # The extended resource that yields /dev/kvm without privileged mode,
        # and the thing ResourceQuota counts to cap concurrency. ARM guests run
        # under TCG on an x86 host, so KVM would be meaningless for them — and
        # asking for it would wrongly consume a slot from the concurrency cap.
        resources["limits"]["devices.kubevirt.io/kvm"] = "1"

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": session_id,
            "labels": {
                "app.kubernetes.io/name": "vmlab",
                "app.kubernetes.io/instance": release,
                "app.kubernetes.io/component": "session",
                "vmlab.zachd/config": slug,
                "vmlab.zachd/session": session_id,
                # The label the sandbox policies select on. A pod that reached
                # here with an unrecognised profile would be unlabelled and
                # therefore unpoliced, which is why the lookup above raises
                # instead of defaulting.
                "vmlab.zachd/network": config["network"],
                # Selects the policy that opens the monitor port. Absent on
                # every other guest, so QMP is not merely closed by firewall
                # but genuinely not listening.
                "vmlab.zachd/savepoints": "true" if savepoints else "false",
            },
            "annotations": {
                "vmlab.zachd/display-name": config["name"],
                # Read back when saving: -loadvm restores VM state onto the
                # devices QEMU currently has, so a snapshot taken with the CD
                # inserted can only be restored with the CD inserted.
                "vmlab.zachd/boot-from-iso": "true" if boot_from_iso else "false",
            },
        },
        "spec": {
            # Never Always: a guest that panics should stay dead and visible,
            # not loop. Combined with activeDeadlineSeconds this is what makes
            # a session genuinely short-lived.
            "restartPolicy": "Never",
            # The hard TTL, enforced by the kubelet rather than by the reaper.
            # It survives the UI being down, restarted, or wrong.
            "activeDeadlineSeconds": ttl,
            "serviceAccountName": settings.vm_service_account,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 15,
            **_affinity(),
            "initContainers": init_containers,
            "containers": [
                {
                    "name": "qemu",
                    "image": image,
                    "imagePullPolicy": settings.vm_pull_policy,
                    "env": env,
                    "ports": (
                        [{"name": "http", "containerPort": 8006}]
                        + ([{"name": "qmp", "containerPort": QMP_PORT}] if savepoints else [])
                    ),
                    "securityContext": {
                        # Root *inside its own container*, because the image's
                        # entrypoint needs it. Not privileged, no host mounts, no
                        # capabilities beyond the two slirp needs, and it holds
                        # no credential. QEMU is the isolation boundary; this is
                        # the fence around QEMU.
                        "allowPrivilegeEscalation": False,
                        "capabilities": capabilities,
                    },
                    "resources": resources,
                    "volumeMounts": mounts,
                }
            ],
            "volumes": volumes,
        },
    }


def build_fetch_job(*, config: dict, job_name: str, release: str) -> dict:
    """A Job that pulls one ISO into the shared cache.

    The only thing in this namespace with internet egress. Keeping it separate
    from the guests is what makes "air-gapped guest booting an ISO that came off
    the internet" a coherent thing rather than a contradiction.
    """
    url = validate_iso_url(config.get("iso", ""))
    if not url:
        raise ValidationError(f"{config['slug']} has no ISO URL to fetch")

    sha = (config.get("sha256") or "").strip().lower()
    if sha and not _sha256_ok(sha):
        raise ValidationError("sha256 must be 64 hex characters")

    # Written to a .part file and moved into place only after the checksum
    # passes, so an interrupted fetch can never be mistaken for a cached ISO.
    #
    # Archives are unwrapped here rather than at boot. A lot of archived OS
    # media ships zipped (ReactOS releases, most archive.org uploads), and
    # "provide a URL" stops being true if half of them need a manual step. The
    # sniff is on the file's magic bytes, not the URL, because archive.org URLs
    # routinely lack a useful extension. Detection is deliberately narrow: only
    # a real PK zip is treated as an archive, and anything else is passed
    # through untouched.
    script = (
        "set -eu\n"
        'dest="/isos/$1"\n'
        'url="$2"\n'
        'want="$3"\n'
        'tmp="$dest.part"\n'
        'work="/isos/.extract-$1"\n'
        'if [ -s "$dest" ]; then echo "already cached: $1"; exit 0; fi\n'
        "apk add --no-cache curl unzip xz p7zip >/dev/null\n"
        # Flush dirty pages while the download runs. Without this, a fast
        # mirror OOMKills the container regardless of how big its limit is:
        # observed on a 40MB/s archive.org fetch, which died at exactly 250MiB
        # written against a 256Mi cgroup, while a slower mirror of a LARGER
        # file sat at 9Mi the whole way. The cost is writeback that would have
        # happened anyway; the benefit is that the failure stops being a
        # function of how fast the far end happens to be today.
        '( while sleep 2; do sync "$tmp" 2>/dev/null || sync; done ) &\n'
        'syncer=$!\n'
        # NO -C -, and a stale partial is removed first. Resume looks like the
        # obvious win on a 7.9GB image and is actively unsafe here: curl fixes
        # the resume offset ONCE at startup, so its own --retry either truncates
        # back to that offset (losing 6GB mid-transfer, observed) or, when a
        # partial from an earlier Job exists, appends the retried bytes past
        # where they belong. The latter is what turned a 7.36GiB image into a
        # 10.98GiB one that cached as healthy. Restarting a big download is
        # merely slow; caching a corrupt one is silent, so the trade is easy.
        # --retry-all-errors is kept because a broken HTTP/2 stream is not in
        # curl's default retry set.
        'rm -f "$tmp"\n'
        'curl -fL --retry 5 --retry-delay 5 --retry-all-errors -o "$tmp" "$url"\n'
        'kill "$syncer" 2>/dev/null || true\n'
        # Size check, and it is not belt-and-braces — it caught a real
        # corruption. With -C - and --retry, a CDN that answers a Range request
        # with the FULL body makes curl append it to the partial file: the
        # 7.36GiB Bazzite image arrived as 10.98GiB and was cached as if fine.
        # Nothing else here would have noticed, because a checksum is optional
        # and most entries have none. A truncated download fails this the same
        # way. On mismatch the partial is removed so the next attempt restarts
        # clean rather than resuming corruption forever.
        # grep/cut rather than awk, for two reasons that both made the awk
        # version a silent no-op: awk printed 7907770368 as "7.90777e+09", and
        # busybox `[ "7.90777e+09" -gt 0 ]` errors out, which inside an `if` is
        # simply skipped. IGNORECASE is also a gawk extension busybox lacks, so
        # an HTTP/1.1 "Content-Length" header would never have matched at all.
        # tail -1 takes the final hop's header, since -L follows redirects.
        'want_size=$(curl -sIL "$url" | tr -d "\\r" | grep -i "^content-length:" | tail -1 | cut -d" " -f2)\n'
        '[ -n "$want_size" ] || want_size=0\n'
        'got_size=$(stat -c %s "$tmp")\n'
        'if [ "$want_size" -gt 0 ] && [ "$got_size" -ne "$want_size" ]; then\n'
        '  rm -f "$tmp"\n'
        '  echo "size mismatch: got $got_size want $want_size (partial discarded)" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ -n "$want" ]; then\n'
        '  got=$(sha256sum "$tmp" | cut -d" " -f1)\n'
        '  if [ "$got" != "$want" ]; then\n'
        '    rm -f "$tmp"\n'
        '    echo "checksum mismatch: got $got want $want" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "fi\n"
        # Unwrap whatever the far end actually served. Detection is on magic
        # bytes, not the URL, because the good sources for old operating
        # systems name files however they like: 9front ships .iso.gz, MINIX
        # .iso.bz2, KolibriOS .7z, FreeDOS .zip, and archive.org URLs routinely
        # carry no extension at all. Anything unrecognised is passed through
        # untouched rather than guessed at.
        'magic=$(dd if="$tmp" bs=1 count=8 2>/dev/null | od -An -tx1 | tr -d " \\n")\n'
        'kind="raw"\n'
        'case "$magic" in\n'
        '  504b*)      kind="zip" ;;\n'
        '  1f8b*)      kind="gz" ;;\n'
        '  425a68*)    kind="bz2" ;;\n'
        '  fd377a585a*) kind="xz" ;;\n'
        '  377abcaf271c*) kind="7z" ;;\n'
        'esac\n'
        'if [ "$kind" != "raw" ]; then\n'
        '  echo "compressed image detected ($kind), unpacking..."\n'
        'fi\n'
        'case "$kind" in\n'
        # Single-file compressors decompress straight to the image.
        '  gz)  gunzip -c "$tmp" > "$dest.out" && mv "$dest.out" "$dest" && rm -f "$tmp" ;;\n'
        '  bz2) bunzip2 -c "$tmp" > "$dest.out" && mv "$dest.out" "$dest" && rm -f "$tmp" ;;\n'
        '  xz)  unxz -c "$tmp" > "$dest.out" && mv "$dest.out" "$dest" && rm -f "$tmp" ;;\n'
        # Containers may hold several files; take the largest .iso/.img.
        '  zip|7z)\n'
        '    rm -rf "$work"; mkdir -p "$work"\n'
        '    if [ "$kind" = "zip" ]; then unzip -q -o "$tmp" -d "$work"; else 7z x -y -o"$work" "$tmp" >/dev/null; fi\n'
        '    inner=""\n'
        '    for f in $(find "$work" -type f \\( -iname "*.iso" -o -iname "*.img" \\)); do\n'
        '      if [ -z "$inner" ] || [ "$(stat -c %s "$f")" -gt "$(stat -c %s "$inner")" ]; then\n'
        '        inner="$f"\n'
        "      fi\n"
        "    done\n"
        '    if [ -z "$inner" ]; then\n'
        '      rm -rf "$work" "$tmp"\n'
        '      echo "archive contains no .iso or .img" >&2\n'
        "      exit 1\n"
        "    fi\n"
        '    echo "using $(basename "$inner")"\n'
        '    mv "$inner" "$dest"; rm -rf "$work" "$tmp" ;;\n'
        '  *)   mv "$tmp" "$dest" ;;\n'
        "esac\n"
        'ls -lh "$dest"\n'
    )

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "labels": {
                "app.kubernetes.io/name": "vmlab",
                "app.kubernetes.io/instance": release,
                "vmlab.zachd/role": "iso-fetch",
                "vmlab.zachd/config": config["slug"],
            },
        },
        "spec": {
            "backoffLimit": 2,
            "ttlSecondsAfterFinished": 600,
            "activeDeadlineSeconds": settings.fetch_timeout_seconds,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "vmlab",
                        # Selected by the iso-fetch NetworkPolicy. Without this
                        # label the Job is unpoliced; with it, it is the one
                        # workload here permitted to leave the cluster.
                        "vmlab.zachd/role": "iso-fetch",
                        "vmlab.zachd/config": config["slug"],
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "containers": [
                        {
                            "name": "fetch",
                            "image": settings.fetch_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c"],
                            # Caller-influenced values travel as positional
                            # arguments, never interpolated into the script.
                            "args": [script, "sh", iso_filename(config["slug"]), url, sha],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": settings.fetch_resources,
                            "volumeMounts": [{"name": "isos", "mountPath": "/isos"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "isos",
                            "persistentVolumeClaim": {"claimName": settings.iso_pvc},
                        }
                    ],
                },
            },
        },
    }


def build_disk_pvc(*, config: dict, release: str, storage_class: str | None = None) -> dict:
    """The opt-in per-config disk for `persist: true`."""
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": f"{int(config['diskGib'])}Gi"}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": disk_pvc_name(release, config["slug"]),
            "labels": {
                "app.kubernetes.io/name": "vmlab",
                "app.kubernetes.io/instance": release,
                "vmlab.zachd/config": config["slug"],
            },
            "annotations": {"k8up.io/backup": "false"},
        },
        "spec": spec,
    }
