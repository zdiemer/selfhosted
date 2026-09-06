"""Save-point plumbing: name validation, QMP parsing, PNG encoding."""

from __future__ import annotations

import struct
import zlib

import pytest

from vmlab import qmp, screenshot, snapshots


@pytest.mark.parametrize("name", ["a b", "x;reboot", "", "A" * 40, "-lead", "a/b", "$(id)"])
def test_bad_savepoint_names_rejected(name):
    with pytest.raises(snapshots.SnapshotError):
        snapshots.validate_name(name)


def test_savepoint_names_are_normalised():
    assert snapshots.validate_name("After_Install") == "after_install"


def _parse(text, monkeypatch):
    """Drive the real parser with captured output, without a socket."""
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def hmp(self, _): return text
    monkeypatch.setattr(qmp, "Qmp", lambda *a, **k: FakeConn())
    return qmp.list_snapshots("10.0.0.1")


def test_info_snapshots_parses_real_qemu_11_output(monkeypatch):
    """Captured verbatim from QEMU 11.1. Note the ID column is "--", not a
    number: an earlier parser required parts[0].isdigit() and therefore
    reported "no save points" for a disk that had one."""
    text = (
        "List of snapshots present on all disks:\r\n"
        "ID      TAG               VM_SIZE                DATE        VM_CLOCK     ICOUNT\r\n"
        "--      setup_screen     3.03 MiB 2026-09-06 08:16:59  0000:01:47.690         --"
    )
    rows = _parse(text, monkeypatch)
    assert [r["name"] for r in rows] == ["setup_screen"]
    assert rows[0]["vmSize"] == "3.03 MiB"


def test_numeric_ids_still_parse(monkeypatch):
    text = (
        "List of snapshots present on all disks:\n"
        "ID        TAG              VM_SIZE      DATE     VM_CLOCK\n"
        "1         after_install    512 MiB 2026-09-06 10:11:12   00:02:03.456\n"
        "2         before_patch     498 MiB 2026-09-06 11:00:00   00:40:00.000\n"
    )
    assert [r["name"] for r in _parse(text, monkeypatch)] == ["after_install", "before_patch"]


def test_explicit_empty_is_believed(monkeypatch):
    assert _parse("There is no snapshot available.", monkeypatch) == []


def test_unreadable_output_raises_rather_than_reporting_empty(monkeypatch):
    """Returning [] for output we cannot read would let listing() prune every
    remembered save point and delete its screenshot — data loss caused by a
    format change."""
    with pytest.raises(qmp.QmpError):
        _parse("Snapshots:\n<some future format nobody has seen>", monkeypatch)


def test_hmp_reports_errors_by_returning_text():
    """The trap this guards: HMP returns an error STRING with a success status,
    so a caller checking only for exceptions would treat failure as success."""
    class FakeConn:
        def __init__(self, out): self.out = out
        def hmp(self, _): return self.out

    with pytest.raises(qmp.QmpError, match="no block device"):
        qmp._hmp_or_raise(FakeConn("Error: no block device can accept snapshots"), "savevm x", "could not save")
    qmp._hmp_or_raise(FakeConn(""), "savevm x", "could not save")  # empty == success


def test_png_encoder_produces_a_decodable_image():
    w, h = 4, 3
    rgb = bytearray()
    for y in range(h):
        for x in range(w):
            rgb += bytes([x * 60 % 256, y * 80 % 256, 128])
    png = screenshot._png(w, h, rgb)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR must describe the right geometry, 8-bit truecolour.
    ihdr_w, ihdr_h, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert (ihdr_w, ihdr_h, depth, colour) == (w, h, 8, 2)

    # Walk the chunks, verify every CRC, and round-trip the pixels back.
    pos, idat = 8, b""
    while pos < len(png):
        ln = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + ln]
        crc = struct.unpack(">I", png[pos + 8 + ln:pos + 12 + ln])[0]
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, f"bad CRC on {tag!r}"
        if tag == b"IDAT":
            idat += data
        pos += 12 + ln

    raw = zlib.decompress(idat)
    stride = w * 3
    out = bytearray()
    for y in range(h):
        row = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        assert row[0] == 0, "only filter type 0 is emitted"
        out += row[1:]
    assert bytes(out) == bytes(rgb)


def test_manifest_survives_a_partial_write(tmp_path, monkeypatch):
    """The UI reads this on every poll; a half-written file would surface as
    'no save points', i.e. as data loss on the page that reports what you have."""
    monkeypatch.setenv("VMLAB_SNAPSHOT_MOUNT", str(tmp_path))
    snapshots.record("winxp", "after_install", png=b"\x89PNG-not-real", width=800, height=600)
    entries = snapshots.listing("winxp")
    assert [e["name"] for e in entries] == ["after_install"]
    assert entries[0]["observed"] is False  # remembered, VM not running
    # A temp file must never be left behind as a visible entry.
    assert not [p for p in tmp_path.glob("winxp/*") if p.name.startswith(".")]


def test_listing_drops_savepoints_qemu_no_longer_has(tmp_path, monkeypatch):
    """A stale row would offer a Load button that fails at boot."""
    monkeypatch.setenv("VMLAB_SNAPSHOT_MOUNT", str(tmp_path))
    snapshots.record("winxp", "gone", png=None)
    snapshots.record("winxp", "kept", png=None)
    live = snapshots.listing("winxp", live=[{"name": "kept", "vmSize": "512 MiB"}])
    assert [e["name"] for e in live] == ["kept"]
    assert live[0]["observed"] is True
    assert [e["name"] for e in snapshots.listing("winxp")] == ["kept"]


def test_catalog_separates_can_save_from_has_saves():
    """Regression: the API overwrote the config's boolean `savepoints` with the
    list of save points, so the UI's "show a Save button" test read undefined."""
    import ast, pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "vmlab" / "main.py"
    text = src.read_text()
    assert '"savepointsEnabled": bool(cfg.get("savepoints"))' in text
    assert '"savepoints": k8s.list_savepoints' in text


def test_savepoint_guests_disable_invtsc():
    """savevm refuses if any device is non-migratable, and qemux/qemu adds
    +invtsc whenever the host TSC is stable — which every node here has. Without
    this the first save fails with "State blocked by non-migratable CPU device"."""
    from vmlab import spec

    cfg = spec.validate_config({"slug": "x", "savepoints": True, "persist": True})
    pod = spec.build_session_pod(
        config=cfg, session_id="vm-x-1", release="vmlab",
        boot_from_iso=False, ttl_seconds=600,
    )
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["CPU_FLAGS"] == "-invtsc"
    # hv_passthrough mirrors the host CPU and so cannot be serialised either.
    assert env["HV"] == "N"


def test_non_savepoint_guests_keep_the_better_clock():
    from vmlab import spec

    pod = spec.build_session_pod(
        config=spec.validate_config({"slug": "x"}), session_id="vm-x-1",
        release="vmlab", boot_from_iso=False, ttl_seconds=600,
    )
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert "CPU_FLAGS" not in env
    assert "HV" not in env


def test_hybrid_iso_save_failure_explains_the_fix():
    """savevm refuses while a writable non-qcow2 device is attached, and hybrid
    ISOs are force-attached writable. QEMU's own wording does not say that, and
    it is the single most likely save failure."""
    from vmlab import k8s

    msg = k8s._explain_save_failure(
        "could not save: Error: Device 'boot' is writable but does not support snapshots",
        "bazzite",
    )
    assert "hybrid" in msg and "without the boot media" in msg


def test_unrelated_save_failures_are_passed_through_verbatim():
    from vmlab import k8s

    assert k8s._explain_save_failure("Error: No space left on device", "x") == (
        "Error: No space left on device"
    )


def test_thumbnail_shrinks_and_stays_decodable():
    from vmlab import screenshot
    w, h = 1280, 720
    rgb = bytearray(w * h * 3)
    for i in range(0, len(rgb), 3):
        rgb[i] = (i // 3) % 256
    png = screenshot.thumbnail(rgb, w, h, max_width=480)
    tw, th = struct.unpack(">II", png[16:24])
    assert tw == 480 and th == 270
    assert len(png) < 200_000
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_thumbnail_averages_rather_than_point_samples():
    """Point-sampling would drop thin strokes entirely — a BIOS screen, which
    is one-pixel text on black, would thumbnail to solid black."""
    from vmlab import screenshot
    w, h = 8, 2
    rgb = bytearray(w * h * 3)
    for y in range(h):                       # every other column white
        for x in range(0, w, 2):
            o = (y * w + x) * 3
            rgb[o] = rgb[o + 1] = rgb[o + 2] = 255
    png = screenshot.thumbnail(rgb, w, h, max_width=4)
    raw = zlib.decompress(b"".join(
        png[p + 8:p + 8 + struct.unpack(">I", png[p:p + 4])[0]]
        for p in _chunks(png) if png[p + 4:p + 8] == b"IDAT"))
    row = raw[1:1 + 4 * 3]
    assert any(0 < v < 255 for v in row), "expected averaged greys, got point samples"


def _chunks(png):
    pos = 8
    while pos < len(png):
        yield pos
        pos += 12 + struct.unpack(">I", png[pos:pos + 4])[0]


def test_small_screens_are_not_upscaled():
    from vmlab import screenshot
    rgb = bytearray(320 * 200 * 3)
    png = screenshot.thumbnail(rgb, 320, 200, max_width=480)
    assert struct.unpack(">II", png[16:24]) == (320, 200)


def test_boot_none_not_empty_string():
    """Regression: BOOT="" is treated by install.sh as UNSET, so it falls back
    to `ENV BOOT="alpine"`, downloads Alpine and attaches it as a writable
    hybrid disk — which then breaks -loadvm with "Device 'boot' is writable but
    does not support snapshots". "none" short-circuits on a disk with data and
    fails loudly (exit 64) on one without."""
    from vmlab import spec

    pod = spec.build_session_pod(
        config=spec.validate_config({"slug": "x", "savepoints": True, "persist": True}),
        session_id="vm-x-1", release="vmlab", boot_from_iso=False, ttl_seconds=600,
    )
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["BOOT"] == "none"


def test_pod_records_how_it_was_booted():
    """A save point can only be restored onto the same device topology, so the
    save path has to know whether the CD was in."""
    from vmlab import spec

    cfg = spec.validate_config({"slug": "x", "savepoints": True, "persist": True})
    for booted in (True, False):
        pod = spec.build_session_pod(
            config=cfg, session_id="vm-x-1", release="vmlab",
            boot_from_iso=booted, ttl_seconds=600,
        )
        ann = pod["metadata"]["annotations"]["vmlab.zachd/boot-from-iso"]
        assert ann == ("true" if booted else "false")


def test_savepoint_remembers_boot_topology(tmp_path, monkeypatch):
    monkeypatch.setenv("VMLAB_SNAPSHOT_MOUNT", str(tmp_path))
    snapshots.record("hm", "live", png=None, boot_from_iso=True)
    entry = snapshots.listing("hm")[0]
    assert entry["bootFromIso"] is True


def test_terminal_session_pods_are_reaped():
    """A Failed pod is invisible to both activeDeadlineSeconds and the idle
    reaper, but still holds the pod quota AND its per-config disk — which
    silently stopped the next launch from getting a fresh disk."""
    import inspect
    from vmlab import k8s

    src = inspect.getsource(k8s.reap_terminal)
    assert '"Failed"' in src and '"Succeeded"' in src
    assert "deletion_timestamp" in src  # don't re-delete what is already going


def test_hybrid_iso_is_flattened_by_default_and_opt_out():
    """Clearing the MBR signature on the private copy is what makes a hybrid
    ISO attach as a read-only CD-ROM. It follows `flattenIso`, NOT `savepoints`
    — it began as a savevm workaround, but a guest looking for a boot CD needs
    it whether or not it can be snapshotted."""
    from vmlab import spec

    on = spec.build_session_pod(
        config=spec.validate_config({"slug": "x", "savepoints": True, "persist": True}),
        session_id="vm-x-1", release="vmlab", boot_from_iso=True, ttl_seconds=600,
    )
    args = on["spec"]["initContainers"][0]["args"]
    assert "seek=510" in args[0], "expected the MBR signature to be cleared"
    assert args[-2] == "yes"   # [-1] is the media type

    # no save points, still flattened
    nosp = spec.build_session_pod(
        config=spec.validate_config({"slug": "x", "bootMode": "legacy"}),
        session_id="vm-x-1", release="vmlab", boot_from_iso=True, ttl_seconds=600,
    )
    assert nosp["spec"]["initContainers"][0]["args"][-2] == "yes"

    # explicit opt-out (Bazzite's isomd5sum, Haiku's anyboot MBR)
    off = spec.build_session_pod(
        config=spec.validate_config({"slug": "x", "flattenIso": False}),
        session_id="vm-x-1", release="vmlab", boot_from_iso=True, ttl_seconds=600,
    )
    assert off["spec"]["initContainers"][0]["args"][-2] == "no"


def test_flatten_never_touches_the_shared_cache():
    """The write must target the private copy, never /isos — otherwise one
    guest would silently modify the ISO every other guest boots."""
    from vmlab import spec

    script = spec.build_session_pod(
        config=spec.validate_config({"slug": "x", "savepoints": True, "persist": True}),
        session_id="vm-x-1", release="vmlab", boot_from_iso=True, ttl_seconds=600,
    )["spec"]["initContainers"][0]["args"][0]
    for line in script.splitlines():
        if "dd of=" in line:
            # the staged copy keeps its real extension, so the path is
            # "/boot/boot.$3" rather than a literal .iso
            assert "/boot/boot." in line and "/isos" not in line


@pytest.mark.parametrize("mode", ["uefi", "secure", "windows_secure"])
def test_uefi_savepoints_refused_at_validation(mode):
    """qemux/qemu backs UEFI variables with a WRITABLE raw pflash drive, and
    savevm refuses whenever any writable non-qcow2 device is attached. Caught at
    validation because finding out after a 40-minute install would be cruel."""
    from vmlab import spec

    with pytest.raises(spec.ValidationError, match="BIOS"):
        spec.validate_config(
            {"slug": "x", "savepoints": True, "persist": True, "bootMode": mode}
        )


def test_legacy_savepoints_allowed():
    from vmlab import spec

    for mode in ("legacy", "windows_legacy"):
        cfg = spec.validate_config(
            {"slug": "x", "savepoints": True, "persist": True, "bootMode": mode}
        )
        assert cfg["savepoints"] is True


def test_uefi_without_savepoints_is_still_fine():
    from vmlab import spec
    assert spec.validate_config({"slug": "x", "bootMode": "uefi"})["bootMode"] == "uefi"


def test_savepoint_guest_defaults_to_bios_rather_than_erroring():
    """Asking for save points without naming firmware should just work — the
    caller never chose UEFI, so failing on it would be an error about a setting
    they did not set."""
    from vmlab import spec

    cfg = spec.validate_config({"slug": "x", "savepoints": True, "persist": True})
    assert cfg["bootMode"] == "legacy"
    assert cfg["savepoints"] is True


def test_plain_guest_still_defaults_to_uefi():
    from vmlab import spec
    assert spec.validate_config({"slug": "x"})["bootMode"] == "uefi"


def test_terminal_pods_are_kept_long_enough_to_debug():
    """Regression: reaping on sight meant a guest that failed to boot vanished
    with the only log explaining why."""
    import inspect
    from vmlab import k8s
    from vmlab.config import settings

    src = inspect.getsource(k8s.reap_terminal)
    assert "terminal_grace_seconds" in src
    assert settings.terminal_grace_seconds >= 600


def test_flatten_can_be_declined_per_config():
    """Clearing the MBR signature rewrites sector 0, which invalidates the
    implanted isomd5sum that Fedora-family media verify at boot — dracut then
    HALTS rather than warning. Such guests must be able to opt out."""
    from vmlab import spec

    cfg = spec.validate_config(
        {"slug": "x", "savepoints": True, "persist": True, "flattenIso": False}
    )
    pod = spec.build_session_pod(
        config=cfg, session_id="vm-x-1", release="vmlab",
        boot_from_iso=True, ttl_seconds=600,
    )
    assert pod["spec"]["initContainers"][0]["args"][-2] == "no"


def test_flatten_defaults_on_for_savepoint_guests():
    from vmlab import spec

    cfg = spec.validate_config({"slug": "x", "savepoints": True, "persist": True})
    pod = spec.build_session_pod(
        config=cfg, session_id="vm-x-1", release="vmlab",
        boot_from_iso=True, ttl_seconds=600,
    )
    assert pod["spec"]["initContainers"][0]["args"][-2] == "yes"


def test_flatten_is_independent_of_savepoints():
    """It began as a savevm workaround, but the real effect is that a hybrid
    ISO attaches as a CD-ROM rather than a USB disk — which is what lets a
    guest looking for a boot CD find one. MINIX has no save points and needs it."""
    from vmlab import spec

    cfg = spec.validate_config({"slug": "minix", "bootMode": "legacy"})
    assert cfg["savepoints"] is False
    args = spec.build_session_pod(config=cfg, session_id="vm-m-1", release="vmlab",
                                  boot_from_iso=True, ttl_seconds=600
                                  )["spec"]["initContainers"][0]["args"]
    assert args[-2] == "yes"
