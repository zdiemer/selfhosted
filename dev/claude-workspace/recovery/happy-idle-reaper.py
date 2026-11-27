#!/usr/bin/env python3
"""Stop Happy sessions nobody has used for days, through the daemon's own API.

Every resumed or phone-spawned session keeps a Happy wrapper (and for Codex an
app-server) resident in the happy-daemon container, ~150-370MB each, whether or
not anyone ever comes back to it. They accumulate until the container is
OOM-killed — and then auto-resume brings every one of them back.

"Idle" is judged from the agent transcript, not the live record: the hook
heartbeats `updatedAt` every five seconds, so that field only proves the process
exists. The transcript's mtime is the last turn. A session that is busy (a turn,
a subagent, or a pending recovery) is never stopped, and one whose transcript
cannot be found is left alone rather than guessed at.

Stopping goes through POST /stop-session on the daemon — the same call the app
makes — so the transcript and Happy session survive and can be resumed from the
app. If the hook did not record the exit, the record is marked stopped here so a
later container restart does not resurrect what was deliberately retired.
"""
import glob
import json
import os
import pathlib
import sys
import time
import urllib.request

CONTAINER = "happy-daemon"


def read(file, default=None):
    try:
        return json.loads(pathlib.Path(file).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def last_activity(home, metadata):
    """Newest mtime across the session's transcript files, or None if unknown."""
    sid, tid = metadata.get("claudeSessionId"), metadata.get("codexThreadId")
    if sid:
        files = glob.glob(f"{home}/.claude/projects/*/{glob.escape(sid)}.jsonl")
        # Subagent transcripts live beside it; their writes are activity too.
        files += glob.glob(f"{home}/.claude/projects/*/{glob.escape(sid)}/**/*.jsonl", recursive=True)
    elif tid:
        files = glob.glob(f"{home}/.codex/sessions/**/*{glob.escape(tid)}*.jsonl", recursive=True)
    else:
        return None
    stamps = []
    for file in files:
        try:
            stamps.append(os.path.getmtime(file))
        except OSError:
            pass
    return max(stamps) if stamps else None


def select_idle(records, daemon_sessions, activity, now, idle_seconds):
    """Pure selection: (sessionId, idle seconds) for sessions safe to stop."""
    result = []
    for record in records:
        sid = record.get("sessionId")
        if (record.get("container") != CONTAINER or record.get("state") != "running"
                or record.get("busy") or sid not in daemon_sessions):
            continue
        last = activity(record.get("metadata") or {})
        if last is None:
            continue
        if now - last >= idle_seconds:
            result.append((sid, now - last))
    return sorted(result, key=lambda item: -item[1])


def daemon_post(port, path, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def mark_stopped(root, session_id, pid):
    """Guard against resurrection if the process died without its exit hook."""
    for file in (root / "live").glob("*.json"):
        record = read(file)
        if record.get("sessionId") != session_id or record.get("state") != "running":
            continue
        if record.get("pid") == pid and not pathlib.Path(f"/proc/{pid}").exists():
            record["state"] = "stopped"
            record["updatedAt"] = time.time() * 1000
            temp = file.with_name(file.name + f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(record))
            temp.chmod(0o600)
            os.replace(temp, file)


def sweep(home, root, idle_seconds, dry_run, log=print):
    state = read(home / ".happy/daemon.state.json")
    port = state.get("httpPort")
    if not port:
        log("happy-idle-reaper: daemon not running; skipping")
        return []
    children = daemon_post(port, "/list", {}).get("children") or []
    pids = {c.get("happySessionId"): c.get("pid") for c in children if c.get("happySessionId")}
    records = [read(file) for file in (root / "live").glob("*.json")]
    chosen = select_idle(records, pids, lambda m: last_activity(home, m), time.time(), idle_seconds)
    for session_id, idle in chosen:
        days = idle / 86400
        if dry_run:
            log(f"happy-idle-reaper: would stop {session_id[:8]} (idle {days:.1f}d)")
            continue
        try:
            ok = daemon_post(port, "/stop-session", {"sessionId": session_id}).get("success")
        except OSError as error:
            log(f"happy-idle-reaper: stop {session_id[:8]} failed ({type(error).__name__})")
            continue
        log(f"happy-idle-reaper: stopped {session_id[:8]} (idle {days:.1f}d)" if ok
            else f"happy-idle-reaper: daemon refused to stop {session_id[:8]}")
        if ok:
            for _ in range(30):
                if not pathlib.Path(f"/proc/{pids[session_id]}").exists():
                    break
                time.sleep(1)
            mark_stopped(root, session_id, pids[session_id])
    return chosen


def main():
    home = pathlib.Path(os.environ["HOME"])
    root = pathlib.Path(os.environ.get("HAPPY_WORKSPACE_RECOVERY_DIR", home / ".happy/workspace-recovery"))
    idle_seconds = float(os.environ.get("HAPPY_IDLE_REAP_DAYS", "3")) * 86400
    interval = int(os.environ.get("HAPPY_IDLE_REAP_INTERVAL_MINUTES", "60")) * 60
    dry_run = os.environ.get("HAPPY_IDLE_REAP_DRY_RUN", "false").lower() == "true" or "--dry-run" in sys.argv
    once = "--once" in sys.argv
    # Let the daemon and any auto-resume settle before the first sweep.
    if not once:
        time.sleep(int(os.environ.get("HAPPY_IDLE_REAP_STARTUP_DELAY_SECONDS", "600")))
    while True:
        try:
            sweep(home, root, idle_seconds, dry_run, log=lambda m: print(m, flush=True))
        except (OSError, ValueError) as error:
            print(f"happy-idle-reaper: sweep failed ({type(error).__name__})", flush=True)
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
