#!/usr/bin/env python3
"""Recover only observed live Happy sessions, preserving Happy + agent IDs.

Runs beside Happy (and in term for terminal-origin sessions). No scanning old
sessions.json history into work; that file is only a credential lookup for a
session recorded by the hook. All launches remain ordinary Happy processes.
"""
import fcntl
import json
import os
import pathlib
import signal
import subprocess
import time
import uuid

PROMPT = (
    "The workspace container restarted while this conversation was working. "
    "Resume the interrupted work from the existing transcript. First inspect "
    "files, running jobs, subagent transcripts and checkpoints. Do not repeat "
    "completed mutations or start duplicate workers. Continue verified remaining "
    "work using the existing permission mode. If the work is already finished "
    "or waiting for user input, report that briefly rather than inventing work."
)


def read(file, default=None):
    try:
        return json.loads(pathlib.Path(file).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def write(file, value):
    file = pathlib.Path(file)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = file.with_name(file.name + f".{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)
    os.replace(temp, file)


def identity(pid):
    try:
        fields = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].strip().split()
        if fields[0] == "Z":
            return None
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except (OSError, IndexError):
        return None


def alive(record, pod_uid):
    return (record.get("podUid") == pod_uid and bool(record.get("identity")) and
            identity(record.get("pid", 0)) == record["identity"])


def launch_spec(record, saved, token, environ):
    """Pure adapter, pinned/tested against Happy 1.2.5's buildResumeLaunch."""
    metadata = {**saved.get("metadata", {}), **{k: v for k, v in record["metadata"].items() if v}}
    flavor = metadata.get("flavor") or ("codex" if metadata.get("codexThreadId") else "claude")
    if flavor == "codex" and metadata.get("codexThreadId"):
        args = ["happy", "codex", "--resume", metadata["codexThreadId"], "--started-by", "daemon"]
    elif flavor == "claude" and metadata.get("claudeSessionId"):
        args = ["happy", "claude", "--happy-starting-mode", "remote", "--started-by", "daemon",
                "--resume", metadata["claudeSessionId"]]
    else:
        raise ValueError("session has no supported Claude/Codex transcript to resume")
    if not saved.get("encryptionKey") or saved.get("encryptionVariant") not in ("legacy", "dataKey"):
        raise ValueError("session reconnect credentials are unavailable")
    cwd = metadata.get("path")
    if not cwd or not pathlib.Path(cwd).is_dir():
        raise ValueError("saved workspace directory is unavailable")
    env = dict(environ)
    env.update({
        "HAPPY_RECONNECT_SESSION_ID": record["sessionId"],
        "HAPPY_RECONNECT_ENCRYPTION_KEY": saved["encryptionKey"],
        "HAPPY_RECONNECT_ENCRYPTION_VARIANT": saved["encryptionVariant"],
        "HAPPY_RECONNECT_SEQ": str(saved.get("seq", 0)),
        "HAPPY_RECONNECT_METADATA_VERSION": str(saved.get("metadataVersion", 0)),
        "HAPPY_RECONNECT_AGENT_STATE_VERSION": str(saved.get("agentStateVersion", 0)),
        "HAPPY_WORKSPACE_RESUME_TOKEN": token,
        "HAPPY_WORKSPACE_RESUME_PROMPT": PROMPT if record.get("busy") else "",
        "HAPPY_WORKSPACE_SETTINGS": json.dumps(record.get("settings", {})),
    })
    # Preserve known settings without granting any new permission.
    for key, flag in (("permissionMode", "--permission-mode"), ("model", "--model")):
        value = record.get("settings", {}).get(key)
        if value:
            args += [flag, value]
    return args, cwd, env


def candidates(root, container, pod_uid, now, max_age):
    result = []
    for file in (root / "live").glob("*.json"):
        record = read(file)
        if record.get("container") != container or record.get("state") not in ("running", "restarting"):
            continue
        if now - record.get("updatedAt", 0) > max_age or alive(record, pod_uid):
            continue
        result.append(record)
    return result


def capture_legacy(root, home, container):
    """One-time bridge from the pre-hook image's explicit preStop marker."""
    if container != "happy-daemon":
        return
    marker = read(home / ".happy/restart-notice.json")
    if time.time() - marker.get("at", 0) > 21600:
        return
    ledger = root / f"{container}-pending.json"
    pending = read(ledger)
    sessions = read(home / ".happy/sessions.json").get("sessions", {})
    for session_id in marker.get("sessions", "").split():
        saved = sessions.get(session_id, {})
        metadata = saved.get("metadata", {})
        if metadata.get("lifecycleState") in ("archived", "archiveRequested"):
            continue
        if not metadata:
            continue
        pending.setdefault(session_id, {
            "record": {"sessionId": session_id, "container": container, "busy": True,
                       "metadata": {k: metadata.get(k) for k in
                                    ("path", "host", "flavor", "claudeSessionId", "codexThreadId")},
                       "updatedAt": marker["at"] * 1000, "state": "restarting"},
            "attempts": 0, "token": str(uuid.uuid4()),
        })
    write(ledger, pending)


def run():
    home = pathlib.Path(os.environ["HOME"])
    root = pathlib.Path(os.environ.get("HAPPY_WORKSPACE_RECOVERY_DIR", home / ".happy/workspace-recovery"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    container = os.environ.get("WORKSPACE_CONTAINER", "happy-daemon")
    pod_uid = os.environ.get("WORKSPACE_POD_UID")
    if len(os.sys.argv) > 1 and os.sys.argv[1] == "capture":
        capture_legacy(root, home, container)
        return
    lock = open(root / f"{container}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    max_age = int(os.environ.get("HAPPY_AUTO_RESUME_MAX_AGE_HOURS", "24")) * 3600_000
    limit = int(os.environ.get("HAPPY_AUTO_RESUME_MAX_ATTEMPTS", "3"))
    ledger_file = root / f"{container}-pending.json"
    pending = read(ledger_file)
    pending = {sid: entry for sid, entry in pending.items()
               if not (root / "receipts" / (entry["token"] + ".json")).exists()}
    # Take a boot snapshot once. A user stopping a session during ordinary
    # operation must not make this supervisor resurrect it five seconds later.
    for record in candidates(root, container, pod_uid, time.time() * 1000, max_age):
        pending.setdefault(record["sessionId"], {"record": record,
                           "attempts": record.get("recoveryCount", 0), "token": str(uuid.uuid4())})
    write(ledger_file, pending)

    def shutdown(*_):
        # Freeze live candidates before Happy handles SIGTERM and marks exits.
        for sid, entry in list(pending.items()):
            if (root / "receipts" / (entry["token"] + ".json")).exists():
                del pending[sid]
        for file in (root / "live").glob("*.json"):
            record = read(file)
            if record.get("container") == container and record.get("state") == "running":
                record["state"] = "restarting"
                record["updatedAt"] = time.time() * 1000
                pending.setdefault(record["sessionId"], {"record": record,
                                   "attempts": record.get("recoveryCount", 0), "token": str(uuid.uuid4())})
        write(ledger_file, pending)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    children = {}
    while True:
        for session_id, entry in list(pending.items()):
            if (root / "receipts" / (entry["token"] + ".json")).exists():
                del pending[session_id]
                write(ledger_file, pending)
                continue
            record = entry["record"]
            # A manually resumed session, or a child surviving a supervisor
            # restart, wins. Never launch a competing process for its transcript.
            current = next((read(p) for p in (root / "live").glob("*.json")
                            if read(p).get("sessionId") == session_id), {})
            if alive(current, pod_uid):
                if current.get("resumeToken") != entry["token"]:
                    del pending[session_id]  # manual takeover wins
                    write(ledger_file, pending)
                continue
            if alive(entry.get("launched", {}), pod_uid):
                continue
            if session_id in children:
                children.pop(session_id).poll()
            if entry["attempts"] >= limit or time.time() * 1000 - record.get("updatedAt", 0) > max_age:
                if entry["attempts"] >= limit and not entry.get("notified") and time.time() >= entry.get("retryAt", 0):
                    try:
                        result = subprocess.run(
                            ["happy", "notify", "-t", "Workspace recovery", "-p",
                             f"Auto-resume paused for session {session_id[:8]} after {limit} attempts. "
                             "Its transcript is preserved; resume it in Happy when ready."],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                        entry["notified"] = result.returncode == 0
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    entry["retryAt"] = time.time() + 300
                    write(ledger_file, pending)
                continue
            if time.time() < entry.get("retryAt", 0):
                continue
            attempts_before = entry["attempts"]
            try:
                saved = read(home / ".happy/sessions.json").get("sessions", {}).get(session_id, {})
                # Honor a deliberate archive even if it happened after our boot snapshot.
                if saved.get("metadata", {}).get("lifecycleState") in ("archiveRequested", "archived"):
                    del pending[session_id]
                    write(ledger_file, pending)
                    continue
                args, cwd, env = launch_spec(record, saved, entry["token"], os.environ)
                entry["attempts"] += 1
                env["HAPPY_WORKSPACE_RECOVERY_COUNT"] = str(entry["attempts"])
                entry["retryAt"] = time.time() + 30 * 2 ** (entry["attempts"] - 1)
                write(ledger_file, pending)  # checkpoint before spawning
                log_path = root / f"{container}-launch.log"
                if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
                    log_path.replace(log_path.with_suffix(".previous.log"))
                log = open(log_path, "ab")
                child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
                log.close()
                children[session_id] = child
                entry["launched"] = {"pid": child.pid, "identity": identity(child.pid), "podUid": pod_uid}
                write(ledger_file, pending)
            except (OSError, ValueError) as error:
                entry["attempts"] = attempts_before + 1
                entry["retryAt"] = time.time() + 60
                # No arguments, credentials or environment in errors/logs.
                entry["error"] = str(error)
                write(ledger_file, pending)
        time.sleep(5)


if __name__ == "__main__":
    run()
