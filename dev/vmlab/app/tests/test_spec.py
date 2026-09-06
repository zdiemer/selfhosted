"""Tests for the security boundary.

These assert the properties spec.py exists to hold, not its incidental shape.
The ones that matter are the negatives: that a caller cannot choose an image,
an identity, a mount, or a way out of the network policy.
"""

from __future__ import annotations

import pytest

from vmlab import spec
from vmlab.config import settings


def _config(**over):
    base = {
        "slug": "testos",
        "name": "Test OS",
        "note": "",
        "iso": "https://example.invalid/test.iso",
        "sha256": "",
        "memoryMib": 1024,
        "cores": 1,
        "diskGib": 8,
        "bootMode": "uefi",
        "diskType": "",
        "arch": "x86",
        "network": "none",
        "persist": False,
    }
    base.update(over)
    return base


def _pod(**over):
    return spec.build_session_pod(
        config=_config(**over),
        session_id="vm-testos-abc123",
        release="vmlab",
        boot_from_iso=True,
        ttl_seconds=3600,
    )


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["", "-bad", "has space", "x" * 32, "../etc", "a/b", "Ünicode", "a b", "."],
)
def test_bad_slugs_rejected(slug):
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": slug})


def test_slug_case_is_normalised_not_rejected():
    # Lowercasing happens before the regex, so "TempleOS" is accepted as
    # "templeos" rather than refused. Asserted because it decides the ISO
    # filename, and a normalisation that silently changed later would orphan
    # every cached ISO.
    assert spec.validate_config({"slug": "TempleOS"})["slug"] == "templeos"


def test_slug_is_the_only_thing_shaping_a_filename():
    # Path traversal via the ISO filename would let a fetch Job write outside
    # the cache; the slug regex is what prevents it, so assert the coupling.
    assert spec.iso_filename("templeos") == "templeos.iso"
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "../../etc/passwd"})


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://x/y.iso", "javascript:alert(1)", "https://x/ a.iso"],
)
def test_bad_iso_urls_rejected(url):
    with pytest.raises(spec.ValidationError):
        spec.validate_iso_url(url)


def test_unknown_network_profile_rejected():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "network": "wide-open"})


def test_unknown_boot_mode_and_disk_type_rejected():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "bootMode": "; rm -rf /"})
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "diskType": "$(id)"})


def test_oversize_requests_are_clamped_not_honoured():
    cfg = spec.validate_config(
        {"slug": "x", "memoryMib": 10**9, "cores": 999, "diskGib": 10**6}
    )
    assert cfg["memoryMib"] == settings.max_memory_mib
    assert cfg["cores"] == settings.max_cores
    assert cfg["diskGib"] == settings.max_disk_gib


def test_duplicate_slug_rejected():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "taken"}, existing_slugs={"taken"})


# --- the pod the caller cannot shape ---------------------------------------


def test_image_comes_from_settings_not_the_request():
    pod = _pod()
    image = pod["spec"]["containers"][0]["image"]
    assert image == f"{settings.vm_image}:{settings.vm_tag}"


def test_arm_uses_the_arm_image_and_never_claims_a_kvm_slot():
    # An ARM guest runs under TCG on an x86 host, so requesting KVM would be
    # meaningless AND would consume one of the quota's concurrency slots.
    pod = _pod(arch="arm")
    assert pod["spec"]["containers"][0]["image"].startswith(settings.vm_image_arm)
    limits = pod["spec"]["containers"][0]["resources"]["limits"]
    assert "devices.kubevirt.io/kvm" not in limits


def test_x86_requests_exactly_one_kvm_device():
    # One per pod is what makes the ResourceQuota a concurrency ceiling.
    limits = _pod()["spec"]["containers"][0]["resources"]["limits"]
    assert limits["devices.kubevirt.io/kvm"] == "1"


def test_session_pod_holds_no_credential():
    pod = _pod()
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["serviceAccountName"] == settings.vm_service_account


def test_never_privileged_and_caps_dropped():
    sc = _pod()["spec"]["containers"][0]["securityContext"]
    assert "privileged" not in sc
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]


def test_none_profile_adds_no_capabilities_at_all():
    sc = _pod(network="none")["spec"]["containers"][0]["securityContext"]
    assert "add" not in sc["capabilities"]


def test_internet_profile_adds_only_setgid_setuid():
    # Measured against the image: NETWORK=slirp starts dnsmasq, which drops
    # group to `dip`. Nothing else is needed — in particular NOT NET_ADMIN.
    if "internet" not in settings.network_profiles:
        pytest.skip("internet profile not configured")
    sc = _pod(network="internet")["spec"]["containers"][0]["securityContext"]
    assert set(sc["capabilities"].get("add", [])) <= {"SETGID", "SETUID"}


def test_network_label_matches_profile_so_a_policy_selects_it():
    # A pod whose label did not match a profile would be selected by no
    # NetworkPolicy and therefore unrestricted.
    for profile in settings.network_profiles:
        pod = _pod(network=profile)
        assert pod["metadata"]["labels"]["vmlab.zachd/network"] == profile


def test_unknown_profile_refuses_to_build_rather_than_defaulting():
    with pytest.raises(spec.ValidationError):
        spec.build_session_pod(
            config=_config(network="ghost"),
            session_id="vm-x-1",
            release="vmlab",
            boot_from_iso=False,
            ttl_seconds=60,
        )


def test_air_gapped_guest_gets_no_nic():
    env = {e["name"]: e["value"] for e in _pod(network="none")["spec"]["containers"][0]["env"]}
    assert env["NETWORK"] == "N"


def test_session_always_has_a_hard_deadline():
    pod = _pod()
    assert pod["spec"]["restartPolicy"] == "Never"
    assert 0 < pod["spec"]["activeDeadlineSeconds"] <= settings.max_ttl_seconds


def test_ttl_is_clamped_to_the_ceiling():
    pod = spec.build_session_pod(
        config=_config(), session_id="vm-x-1", release="vmlab",
        boot_from_iso=False, ttl_seconds=10**9,
    )
    assert pod["spec"]["activeDeadlineSeconds"] == settings.max_ttl_seconds


def test_vm_container_never_mounts_the_shared_iso_cache():
    # The initContainer stages a private copy; the guest must not be able to
    # reach the ISOs every other guest boots from.
    pod = _pod()
    qemu = pod["spec"]["containers"][0]
    assert not any(m["name"] == "isos" for m in qemu["volumeMounts"])
    init = pod["spec"]["initContainers"][0]
    isos = next(m for m in init["volumeMounts"] if m["name"] == "isos")
    assert isos["readOnly"] is True


def test_boot_iso_is_mounted_where_findbootfile_looks():
    # install.sh's findBootFile() scans / at maxdepth 1 and short-circuits
    # before BOOT is read. Anywhere else and the image downloads instead.
    mount = next(m for m in _pod()["spec"]["containers"][0]["volumeMounts"] if m["name"] == "bootiso")
    assert mount["mountPath"] == "/boot.iso"
    assert mount["subPath"] == "boot.iso"


def test_without_an_iso_boot_is_none_not_empty():
    # The image bakes ENV BOOT="alpine". This test originally asserted BOOT ==
    # "" and was WRONG: install.sh treats empty as unset and falls back to that
    # default, so a guest meant to boot its own disk downloaded Alpine instead
    # and attached it as a writable hybrid disk. "none" short-circuits on a
    # disk that has data, and fails loudly on one that does not.
    pod = spec.build_session_pod(
        config=_config(persist=True), session_id="vm-x-1", release="vmlab",
        boot_from_iso=False, ttl_seconds=600,
    )
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["BOOT"] == "none"
    assert pod["spec"]["initContainers"] == []


def test_nginx_log_dir_is_shadowed():
    # error.log is www-data:adm 0640; root without CAP_DAC_OVERRIDE cannot
    # write it and the container dies before QEMU starts.
    mounts = {m["mountPath"] for m in _pod()["spec"]["containers"][0]["volumeMounts"]}
    assert "/var/log/nginx" in mounts


def test_memory_request_covers_the_guest_plus_overhead():
    pod = _pod(memoryMib=2048)
    req = pod["spec"]["containers"][0]["resources"]["requests"]["memory"]
    assert req == f"{2048 + settings.overhead_memory_mib}Mi"


def test_laptop_nodes_are_excluded_by_a_required_term():
    # zachd-ubuntu-laptop-2 advertises kvm=1k and carries NO taint, so
    # requesting the KVM device does not exclude it on its own. This must be a
    # *required* term, not a preference.
    affinity = _pod()["spec"]["affinity"]["nodeAffinity"]
    terms = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    exprs = [e for t in terms for e in t["matchExpressions"]]
    assert {"key": "device-type", "operator": "NotIn", "values": ["laptop"]} in exprs


def test_node_constraint_survives_a_missing_env_var():
    # The failure mode this guards: an empty default would drop the constraint
    # entirely and schedule guests onto the laptop, silently.
    assert settings.exclude_node_labels, "exclude_node_labels must never default to empty"


# --- fetch job -------------------------------------------------------------


def test_fetch_job_carries_the_label_its_policy_selects():
    job = spec.build_fetch_job(config=_config(), job_name="f1", release="vmlab")
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels["vmlab.zachd/role"] == "iso-fetch"


def test_fetch_job_passes_url_as_argv_not_interpolated():
    # A URL spliced into the script body would be a shell injection; it must
    # arrive as a positional parameter.
    url = "https://example.invalid/a.iso"
    job = spec.build_fetch_job(config=_config(iso=url), job_name="f1", release="vmlab")
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert url not in args[0]
    assert url in args[1:]


def test_fetch_job_unwraps_archives_by_magic_not_by_url():
    # archive.org URLs routinely lack a useful extension, so sniffing the URL
    # would miss real archives and mis-fire on plain ISOs whose URL happens to
    # say "zip". The script must branch on magic bytes.
    job = spec.build_fetch_job(config=_config(), job_name="f1", release="vmlab")
    script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "magic=" in script and "504b" in script
    assert "unzip" in script


def test_fetch_job_checksums_the_download_before_extracting():
    # The checksum must cover the bytes that were fetched, not the file pulled
    # out of them — otherwise a tampered archive passes and its payload is
    # trusted.
    script = spec.build_fetch_job(
        config=_config(), job_name="f1", release="vmlab"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert script.index("sha256sum") < script.index("unzip -q")


def test_fetch_job_refuses_a_malformed_checksum():
    with pytest.raises(spec.ValidationError):
        spec.build_fetch_job(
            config=_config(sha256="not-a-hash"), job_name="f1", release="vmlab"
        )


def test_fetch_job_needs_a_url():
    with pytest.raises(spec.ValidationError):
        spec.build_fetch_job(config=_config(iso=""), job_name="f1", release="vmlab")


# --- the ISO cache is read from disk, not inferred from Job objects ---------


def test_iso_presence_does_not_depend_on_a_job_surviving(tmp_path, monkeypatch):
    """Regression: iso_status once inferred "cached" from a succeeded fetch Job.

    Jobs carry ttlSecondsAfterFinished, so that signal reliably disappeared ten
    minutes after every fetch and launch() then refused to start anything. The
    lab broke itself on a timer. Presence must come from the volume.
    """
    from vmlab import k8s

    monkeypatch.setattr(k8s, "ISO_MOUNT", str(tmp_path))
    assert k8s.iso_present("ghost") is False

    (tmp_path / "templeos.iso").write_bytes(b"x" * 32)
    assert k8s.iso_present("templeos") is True


def test_empty_iso_file_is_not_cached(tmp_path, monkeypatch):
    # A zero-byte file is what an interrupted copy leaves behind; treating it
    # as cached would boot a guest off nothing.
    from vmlab import k8s

    monkeypatch.setattr(k8s, "ISO_MOUNT", str(tmp_path))
    (tmp_path / "half.iso").write_bytes(b"")
    assert k8s.iso_present("half") is False


def test_fetch_job_validates_size_against_content_length():
    """Regression: -C - plus a CDN that ignores Range appended a full body to a
    partial file. The 7.36GiB Bazzite image cached as 10.98GiB and nothing
    noticed, because sha256 is optional and most entries carry none."""
    script = spec.build_fetch_job(
        config=_config(), job_name="f1", release="vmlab"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "content-length" in script.lower()
    assert "size mismatch" in script
    # NOT awk: it renders large byte counts as "7.90777e+09", and busybox
    # `[ "7.90777e+09" -gt 0 ]` errors out — which inside an `if` is skipped,
    # making the whole check a silent no-op. IGNORECASE is gawk-only too.
    assert "awk" not in script
    # An absent header must not leave want_size empty, or the numeric test errors.
    assert 'want_size=0' in script
    # The partial must be discarded, or the next run resumes the corruption.
    idx = script.index("size mismatch")
    assert 'rm -f "$tmp"' in script[idx - 200:idx]


def test_fetch_job_never_resumes_a_partial_download():
    """Regression: `curl -C -` fixes its resume offset once at startup, so its
    own --retry either truncates back to that offset or appends retried bytes
    past where they belong. The second turned a 7.36GiB image into 10.98GiB
    that cached as healthy. Restarting is slow; corrupting is silent."""
    script = spec.build_fetch_job(
        config=_config(), job_name="f1", release="vmlab"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "-C -" not in script
    # A stale partial from an earlier Job must not be inherited either.
    assert script.index('rm -f "$tmp"') < script.index("curl -fL")


def test_concurrent_fetch_for_one_slug_is_refused(monkeypatch):
    """Two fetch Jobs for one slug write the same .part and interleave into
    garbage. The UI hides the button, but a double-click, a stale tab or a
    direct API call all bypass that."""
    from vmlab import k8s

    monkeypatch.setattr(k8s, "get_config", lambda s: _config(slug=s))
    monkeypatch.setattr(k8s, "iso_status", lambda s: "fetching")
    with pytest.raises(spec.ValidationError, match="already running"):
        k8s.start_fetch("bazzite")


# --- save points -----------------------------------------------------------


def test_savepoints_require_persistence():
    """An emptyDir disk dies with the pod, taking every save point with it.
    validate_config ANDs the two so the UI cannot offer one that evaporates."""
    cfg = spec.validate_config({"slug": "x", "savepoints": True, "persist": False})
    assert cfg["savepoints"] is False
    cfg = spec.validate_config({"slug": "x", "savepoints": True, "persist": True})
    assert cfg["savepoints"] is True


def test_savepoint_config_uses_qcow2_and_opens_a_monitor():
    # savevm writes VM state INTO the disk image; raw has nowhere to put it.
    pod = _pod(savepoints=True, persist=True)
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["DISK_FMT"] == "qcow2"
    assert env["QMP"] == str(spec.QMP_PORT)
    ports = {p["name"] for p in pod["spec"]["containers"][0]["ports"]}
    assert "qmp" in ports


def test_guests_without_savepoints_have_no_monitor_at_all():
    # Not merely firewalled off — not listening. A closed port is a weaker
    # claim than an absent one.
    pod = _pod()
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert "QMP" not in env
    assert {p["name"] for p in pod["spec"]["containers"][0]["ports"]} == {"http"}
    assert pod["metadata"]["labels"]["vmlab.zachd/savepoints"] == "false"


@pytest.mark.parametrize(
    "name",
    ["a b", "x;reboot", "--append", "$(id)", "a/b", "", "A" * 40, "-lead"],
)
def test_loadvm_name_injection_is_refused(name):
    """`-loadvm <name>` lands in ARGUMENTS, which entry.sh expands UNQUOTED
    into the qemu argv, so a space or semicolon is argument injection."""
    with pytest.raises((spec.ValidationError, Exception)):
        spec.build_session_pod(
            config=_config(savepoints=True, persist=True),
            session_id="vm-x-1", release="vmlab",
            boot_from_iso=False, ttl_seconds=600, load_snapshot=name,
        )


def test_loadvm_is_passed_through_for_a_valid_name():
    pod = spec.build_session_pod(
        config=_config(savepoints=True, persist=True),
        session_id="vm-x-1", release="vmlab",
        boot_from_iso=False, ttl_seconds=600, load_snapshot="after_install",
    )
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["ARGUMENTS"] == "-loadvm after_install"


def test_cannot_loadvm_on_a_config_without_savepoints():
    with pytest.raises(spec.ValidationError):
        spec.build_session_pod(
            config=_config(), session_id="vm-x-1", release="vmlab",
            boot_from_iso=False, ttl_seconds=600, load_snapshot="snap",
        )


def test_machine_type_is_allow_listed():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "machine": "pc,acpi=off -drive file=/etc/passwd"})
    assert spec.validate_config({"slug": "x", "machine": "pc"})["machine"] == "pc"


def test_machine_reaches_the_guest_only_when_set():
    pod = _pod(machine="pc")
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["MACHINE"] == "pc"
    env = {e["name"]: e["value"] for e in _pod()["spec"]["containers"][0]["env"]}
    assert "MACHINE" not in env


def test_media_type_is_allow_listed_and_optional():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "mediaType": "ide -drive file=/etc/shadow"})
    assert spec.validate_config({"slug": "x", "mediaType": "auto"})["mediaType"] == "auto"
    env = {e["name"]: e["value"] for e in _pod(mediaType="auto")["spec"]["containers"][0]["env"]}
    assert env["MEDIA_TYPE"] == "auto"
    assert "MEDIA_TYPE" not in {e["name"] for e in _pod()["spec"]["containers"][0]["env"]}


@pytest.mark.parametrize(
    "magic,tool",
    [("1f8b", "gunzip"), ("425a68", "bunzip2"), ("fd377a585a", "unxz"),
     ("504b", "unzip"), ("377abcaf271c", "7z x")],
)
def test_fetch_job_handles_every_published_container(magic, tool):
    """The good sources for old operating systems name files however they like:
    9front ships .iso.gz, MINIX .iso.bz2, KolibriOS .7z, FreeDOS .zip, and
    archive.org URLs often have no extension. Detection is on magic bytes."""
    script = spec.build_fetch_job(
        config=_config(), job_name="f1", release="vmlab"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert magic in script and tool in script


def test_decompression_happens_after_the_integrity_checks():
    """Size and checksum must cover the bytes that were FETCHED, not what was
    unpacked from them — otherwise a tampered archive passes and its payload is
    trusted."""
    script = spec.build_fetch_job(
        config=_config(), job_name="f1", release="vmlab"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert script.index("want_size") < script.index("magic=")
    assert script.index("sha256sum") < script.index("magic=")


def test_numeric_leading_slug_is_allowed():
    """9front. The regex permits a leading digit; asserted because the slug
    also becomes a filename and a Kubernetes label value."""
    assert spec.validate_config({"slug": "9front"})["slug"] == "9front"


def test_config_omitting_optional_fields_is_launchable():
    """Regression: seed entries from values.yaml were used raw, so an entry that
    omitted bootMode raised KeyError inside build_session_pod at LAUNCH — and
    silently skipped the defaulting that gives save-point guests BIOS."""
    minimal = {"slug": "kolibrios", "name": "KolibriOS", "iso": "https://e.invalid/k.iso",
               "memoryMib": 256, "cores": 1, "diskGib": 1,
               "persist": True, "savepoints": True, "network": "none"}
    cfg = spec.validate_config(minimal)
    assert cfg["bootMode"] == "legacy"     # not uefi: savevm needs BIOS
    pod = spec.build_session_pod(
        config=cfg, session_id="vm-k-1", release="vmlab",
        boot_from_iso=True, ttl_seconds=600,
    )
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["BOOT_MODE"] == "legacy"


def test_vga_is_allow_listed_and_optional():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "vga": "vga -netdev user,id=n0"})
    assert spec.validate_config({"slug": "x", "vga": "vga"})["vga"] == "vga"
    env = {e["name"]: e["value"] for e in _pod(vga="vga")["spec"]["containers"][0]["env"]}
    assert env["VGA"] == "vga"
    assert "VGA" not in {e["name"] for e in _pod()["spec"]["containers"][0]["env"]}


def test_boot_media_keeps_its_real_extension():
    """install.sh keys on the extension, so a floppy image staged as boot.iso
    would be mis-detected. Visopsys ships only a 1.44MB .img."""
    cfg = spec.validate_config({"slug": "visopsys", "bootMedia": "img",
                                "savepoints": True, "persist": True})
    pod = spec.build_session_pod(config=cfg, session_id="vm-v-1", release="vmlab",
                                 boot_from_iso=True, ttl_seconds=600)
    mount = next(m for m in pod["spec"]["containers"][0]["volumeMounts"] if m["name"] == "bootiso")
    assert mount["mountPath"] == "/boot.img" and mount["subPath"] == "boot.img"
    assert spec.iso_filename("visopsys", "img") == "visopsys.img"


def test_flatten_only_applies_to_isos():
    """Zeroing offset 510 of a floppy image would corrupt its boot sector."""
    cfg = spec.validate_config({"slug": "v", "bootMedia": "img",
                                "savepoints": True, "persist": True})
    script = spec.build_session_pod(config=cfg, session_id="vm-v-1", release="vmlab",
                                    boot_from_iso=True, ttl_seconds=600
                                    )["spec"]["initContainers"][0]["args"][0]
    assert '[ "$3" = "iso" ]' in script


def test_unknown_boot_media_rejected():
    with pytest.raises(spec.ValidationError):
        spec.validate_config({"slug": "x", "bootMedia": "vmdk"})


def test_zstd_is_handled():
    """Redox publishes .iso.zst / .img.zst."""
    script = spec.build_fetch_job(config=_config(), job_name="f", release="vmlab"
                                  )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "28b52ffd" in script and "unzstd" in script


def test_fetch_job_refuses_an_html_error_page():
    """A mirror that answers a missing file with 200 + HTML would otherwise be
    cached as a disk image — and the size check cannot catch it, because the
    size matches the HTML exactly. Observed with an AROS nightly mirror."""
    script = spec.build_fetch_job(config=_config(), job_name="f", release="vmlab"
                                  )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "text/html" in script and "not an image" in script
    # must be judged before the size check, which would otherwise pass
    assert script.index("ctype=") < script.index("want_size=")


def test_floppy_images_go_to_fda_and_are_not_also_a_disk():
    """A 1.44MB floppy attached as a hard disk never runs its boot sector — it
    expects floppy geometry and to be booted as A:. Visopsys ships only that."""
    cfg = spec.validate_config({"slug": "visopsys", "bootMedia": "img", "floppy": True,
                                "savepoints": True, "persist": True})
    pod = spec.build_session_pod(config=cfg, session_id="vm-v-1", release="vmlab",
                                 boot_from_iso=True, ttl_seconds=600)
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["ARGUMENTS"] == "-fda /boot.img"
    # DISK_TYPE, not MEDIA_TYPE: install.sh routes a .img through the disk
    # path, so MEDIA_TYPE is never consulted and QEMU opened the file twice,
    # dying with 'Failed to get "write" lock'.
    assert env["DISK_TYPE"] == "none"


def test_floppy_and_loadvm_compose():
    cfg = spec.validate_config({"slug": "v", "bootMedia": "img", "floppy": True,
                                "savepoints": True, "persist": True})
    pod = spec.build_session_pod(config=cfg, session_id="vm-v-1", release="vmlab",
                                 boot_from_iso=True, ttl_seconds=600,
                                 load_snapshot="after_boot")
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["ARGUMENTS"] == "-fda /boot.img -loadvm after_boot"


def test_usb_can_be_disabled():
    """Visopsys stalls its hardware scan on the emulated USB tablet."""
    cfg = spec.validate_config({"slug": "x", "usb": False})
    pod = spec.build_session_pod(config=cfg, session_id="vm-x-1", release="vmlab",
                                 boot_from_iso=False, ttl_seconds=600)
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["USB"] == "N"
    # unset must leave the image's own default alone rather than forcing it on
    plain = spec.build_session_pod(config=spec.validate_config({"slug": "x"}),
                                   session_id="vm-x-1", release="vmlab",
                                   boot_from_iso=False, ttl_seconds=600)
    assert "USB" not in {e["name"] for e in plain["spec"]["containers"][0]["env"]}


def test_disk_type_none_is_allowed():
    """The only way to leave a guest with no data disk — which MINIX needs, so
    that its boot CD becomes c0d0 instead of the disk."""
    assert spec.validate_config({"slug": "x", "diskType": "none"})["diskType"] == "none"
