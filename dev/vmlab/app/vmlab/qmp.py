"""A minimal QMP client — enough to drive save points, and nothing more.

WHY TCP AND NOT A SIDECAR. Saving a VM needs to reach QEMU's monitor, which
lives inside the session pod. The obvious design is a sidecar sharing a unix
socket; this instead points qemux/qemu's own `QMP` variable at a TCP port and
talks to it from the UI pod. That removes an entire container, a shared
emptyDir, and a root-running sidecar (a unix QMP socket is created 0755 by
root, so a non-root sidecar could not connect to it anyway).

WHAT THAT COSTS, AND HOW IT IS PAID. A reachable QMP port is total control of
that VM — it can write files, change devices and migrate state. So session pods
now ALWAYS get an ingress NetworkPolicy admitting only the vmlab pod, including
on the `full` profile, which previously had no policy at all. `full` means
unrestricted *egress*; it never meant "anything in the cluster may drive this
VM's monitor". See templates/networkpolicy.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import socket

logger = logging.getLogger(__name__)

QMP_PORT = int(os.environ.get("VMLAB_QMP_PORT", "4444"))
_TIMEOUT = 20.0


class QmpError(RuntimeError):
    """QEMU refused a command, or the monitor is unreachable."""


class Qmp:
    """One short-lived connection per operation.

    Deliberately not pooled: these are rare, human-initiated actions, and a
    stale pooled socket to a VM that has since been deleted is a much more
    annoying failure than reconnecting.
    """

    def __init__(self, host: str, port: int = QMP_PORT, timeout: float = _TIMEOUT):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self) -> "Qmp":
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise QmpError(f"cannot reach the VM monitor: {exc}") from exc
        self._sock.settimeout(self.timeout)
        greeting = self._read()
        if "QMP" not in greeting:
            raise QmpError(f"not a QMP endpoint: {greeting!r}")
        # Until capabilities are negotiated QEMU rejects every other command.
        self.execute("qmp_capabilities")
        return self

    def __exit__(self, *_exc) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _read(self) -> dict:
        """QMP is newline-delimited JSON, but asynchronous *events* are
        interleaved with replies. Events carry no "return"/"error" key, so they
        are skipped rather than mistaken for the answer."""
        while True:
            while b"\n" not in self._buf:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise QmpError("monitor closed the connection")
                self._buf += chunk
            line, _, self._buf = self._buf.partition(b"\n")
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in msg:
                continue
            return msg

    def execute(self, command: str, **arguments) -> object:
        payload = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        self._sock.sendall(json.dumps(payload).encode() + b"\n")
        reply = self._read()
        if "error" in reply:
            raise QmpError(reply["error"].get("desc", str(reply["error"])))
        return reply.get("return")

    def hmp(self, command_line: str) -> str:
        """Run a human-monitor command.

        savevm/loadvm/delvm have no native QMP equivalent, so they go through
        this. Note the failure mode it hides: HMP reports errors by RETURNING
        TEXT with a zero status, so an empty string means success and anything
        else is an error that `execute` would not have raised.
        """
        out = self.execute("human-monitor-command", **{"command-line": command_line})
        return (out or "").strip()


def _hmp_or_raise(conn: Qmp, command_line: str, what: str) -> None:
    out = conn.hmp(command_line)
    if out:
        raise QmpError(f"{what}: {out}")


def save(host: str, name: str) -> None:
    """Freeze the guest, snapshot RAM + disk, resume.

    Stopping first is what makes the save point coherent with the screenshot
    taken alongside it — and savevm on a running guest is slower and can catch
    a disk mid-write.
    """
    with Qmp(host) as conn:
        was_running = bool((conn.execute("query-status") or {}).get("running"))
        if was_running:
            conn.execute("stop")
        try:
            _hmp_or_raise(conn, f"savevm {name}", "could not save")
        finally:
            # Resume even if the save failed: leaving somebody's desktop frozen
            # because a snapshot did not work is a worse outcome than the
            # failed snapshot.
            if was_running:
                try:
                    conn.execute("cont")
                except QmpError:
                    logger.exception("failed to resume after savevm")


def delete(host: str, name: str) -> None:
    with Qmp(host) as conn:
        _hmp_or_raise(conn, f"delvm {name}", "could not delete")


def list_snapshots(host: str) -> list[dict]:
    """Ask QEMU what it actually holds.

    The manifest on disk is only a cache for screenshots and for VMs that are
    not running; when a VM IS running this is the truth, so the two are
    reconciled in snapshots.py rather than trusting the cache.
    """
    with Qmp(host) as conn:
        text = conn.hmp("info snapshots")

    # QEMU says so explicitly, and that is the ONLY way an empty list is
    # believed — see the raise below for why that matters.
    if not text or "no snapshot" in text.lower():
        return []

    out: list[dict] = []
    for line in text.replace("\r", "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        # A real row is <id> <tag> <size> <unit> <date> ... — so the third
        # field is the VM size, and being a number is what distinguishes a row
        # from the banner, the column header, or anything else QEMU prints.
        #
        # NOT `parts[0].isdigit()`, which is what an earlier version keyed on:
        # QEMU 11.1 prints the ID column as "--", so that test skipped every
        # real row and reported "no save points" for a disk that had one.
        try:
            float(parts[2])
        except ValueError:
            continue
        size = parts[2]
        if len(parts) > 3 and parts[3].isalpha():
            size = f"{parts[2]} {parts[3]}"
        out.append({"id": parts[0], "name": parts[1], "vmSize": size})

    if not out:
        # Output we could not read is NOT evidence of an empty list. Returning
        # [] here would let snapshots.listing() prune every remembered save
        # point and delete its screenshot — losing data because of a format
        # change. Fail instead, and let the caller fall back to the manifest.
        raise QmpError(f"could not read the snapshot list: {text[:200]!r}")
    return out


def is_reachable(host: str) -> bool:
    try:
        with Qmp(host, timeout=5) as conn:
            conn.execute("query-status")
        return True
    except (QmpError, OSError):
        return False
