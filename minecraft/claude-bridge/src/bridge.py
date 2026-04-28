"""
Minecraft <-> Claude Code bridge.

Runs as a single long-lived process inside the cluster. It:
  1. Streams the Minecraft pod's stdout via the Kubernetes API and parses
     vanilla chat lines for `<player> !claude <prompt>`.
  2. Spawns `claude -p --output-format json` per request, scoped via a
     baked-in settings.json that denies Bash/Edit/Write and whitelists the
     feature-request MCP tool.
  3. Sends the reply back to all players via RCON `tellraw`, chunked to fit
     a single chat line.

Failure modes that matter:
  - Minecraft pod restarts: the log stream EOFs; we re-resolve the pod by
    label and re-attach without dropping.
  - RCON socket dies: mctools raises; we reconnect on next send.
  - Claude subprocess hangs: hard timeout per request.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from mctools import RCONClient

log = logging.getLogger("bridge")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ---------------------------------------------------------------------------
# Config — populated from env (set by the Deployment from values.yaml).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    mc_namespace: str
    mc_pod_selector: str
    mc_pod_container: str
    rcon_host: str
    rcon_port: int
    rcon_password: str

    max_prompt_chars: int
    max_response_chars: int
    rate_limit_requests: int
    rate_limit_window: int
    enforce_whitelist: bool
    model: str

    system_prompt: str
    feedback_repo_url: str
    feedback_branch: str
    feedback_file: str
    feedback_git_user: str
    feedback_git_email: str
    github_token: str

    state_dir: Path
    request_timeout: int

    @classmethod
    def from_env(cls) -> "Config":
        def need(k: str) -> str:
            v = os.environ.get(k)
            if not v:
                sys.exit(f"missing required env var: {k}")
            return v

        return cls(
            mc_namespace=need("MC_NAMESPACE"),
            mc_pod_selector=need("MC_POD_SELECTOR"),
            mc_pod_container=need("MC_POD_CONTAINER"),
            rcon_host=need("RCON_HOST"),
            rcon_port=int(os.environ.get("RCON_PORT", "25575")),
            rcon_password=need("RCON_PASSWORD"),
            max_prompt_chars=int(os.environ.get("MAX_PROMPT_CHARS", "500")),
            max_response_chars=int(os.environ.get("MAX_RESPONSE_CHARS", "800")),
            rate_limit_requests=int(os.environ.get("RATE_LIMIT_REQUESTS", "5")),
            rate_limit_window=int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60")),
            enforce_whitelist=os.environ.get("ENFORCE_WHITELIST", "true").lower() == "true",
            model=os.environ.get("CLAUDE_MODEL", "sonnet"),
            system_prompt=need("SYSTEM_PROMPT"),
            feedback_repo_url=need("FEEDBACK_REPO_URL"),
            feedback_branch=os.environ.get("FEEDBACK_BRANCH", "main"),
            feedback_file=os.environ.get("FEEDBACK_FILE", "FEEDBACK.md"),
            feedback_git_user=os.environ.get("FEEDBACK_GIT_USER", "claude-bridge"),
            feedback_git_email=os.environ.get("FEEDBACK_GIT_EMAIL", "claude-bridge+bot@local"),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            state_dir=Path(os.environ.get("STATE_DIR", "/app/state")),
            request_timeout=int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120")),
        )


CFG = Config.from_env()


# ---------------------------------------------------------------------------
# RCON — wraps mctools with reconnect-on-failure. Minecraft RCON drops idle
# connections silently; we just reconnect lazily when a send fails.
# ---------------------------------------------------------------------------
class RCON:
    def __init__(self, host: str, port: int, password: str) -> None:
        self.host, self.port, self.password = host, port, password
        self._client: RCONClient | None = None
        self._lock = threading.Lock()

    def _connect(self) -> RCONClient:
        # We send tellraw (already JSON, no color processing needed) and read
        # whitelist/list output (better with codes stripped). REMOVE strips
        # both Minecraft §-codes and ANSI escapes from responses.
        c = RCONClient(self.host, self.port, format_method=RCONClient.REMOVE)
        if not c.login(self.password):
            raise RuntimeError("RCON login failed")
        return c

    def send(self, command: str) -> str:
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._client is None:
                        self._client = self._connect()
                    return self._client.command(command)
                except Exception as e:
                    log.warning("RCON send failed (attempt %d): %s", attempt, e)
                    try:
                        if self._client:
                            self._client.stop()
                    except Exception:
                        pass
                    self._client = None
            raise RuntimeError("RCON send failed after retry")


# ---------------------------------------------------------------------------
# Whitelist — refreshed lazily from RCON `whitelist list`. The output format
# on vanilla / Fabric is one of:
#   "There are no whitelisted players"
#   "There are 3 whitelisted players: Alice, Bob, Charlie"
# ---------------------------------------------------------------------------
class Whitelist:
    REFRESH_SECONDS = 60

    def __init__(self, rcon: RCON) -> None:
        self.rcon = rcon
        self._names: set[str] = set()
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def contains(self, name: str) -> bool:
        with self._lock:
            if time.time() - self._fetched_at > self.REFRESH_SECONDS:
                self._refresh()
            return name.lower() in self._names

    def _refresh(self) -> None:
        try:
            out = self.rcon.send("whitelist list")
        except Exception as e:
            log.warning("whitelist refresh failed, keeping cached set: %s", e)
            return
        # Vanilla / Fabric output is e.g.
        #   "There are 5 whitelisted player(s): Alice, Bob, Charlie"
        # The literal "(s)" trips a naive `players` match.
        m = re.search(r"whitelisted player\(s\)\s*:\s*(.+)", out)
        names = {n.strip().lower() for n in m.group(1).split(",") if n.strip()} if m else set()
        self._names = names
        self._fetched_at = time.time()
        log.info("whitelist refreshed: %d players", len(names))


# ---------------------------------------------------------------------------
# Rate limiter — sliding window per player.
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, requests: int, window_seconds: int) -> None:
        self.n = requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            cutoff = now - self.window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


# ---------------------------------------------------------------------------
# Session store — maps player UUID -> Claude Code session_id, persisted as
# a JSON file on the PVC so sessions survive bridge restarts.
# ---------------------------------------------------------------------------
class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception as e:
                log.warning("session store unreadable, starting fresh: %s", e)
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, session_id: str) -> None:
        with self._lock:
            self._data[key] = session_id
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Claude Code wrapper — one subprocess per request. The MCP config and
# settings.json baked into the image already deny Bash/Edit/Write and
# register the feature-request MCP server, so we don't pass tool flags here.
# ---------------------------------------------------------------------------
class Claude:
    def __init__(self, sessions: SessionStore, system_prompt: str, model: str, timeout: int) -> None:
        self.sessions = sessions
        self.system_prompt = system_prompt
        self.model = model
        self.timeout = timeout

    def ask(self, player_uuid: str, player_name: str, prompt: str,
            on_progress: Callable[[str], None] | None = None) -> str:
        """
        Run a Claude turn with streaming output. on_progress is called for
        every tool_use the model emits — callers use this to update a
        bossbar / status indicator while the agent works through multi-tool
        questions like "find iron in my base and tell me if it's enough
        for a beacon".
        """
        session_id = self.sessions.get(player_uuid)
        # Prompt goes via stdin — claude's --mcp-config is variadic, so a
        # positional prompt arg gets swallowed by it. stdin sidesteps the
        # whole flag-ordering problem.
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--input-format", "text",
            "--verbose",   # stream-json requires --verbose to emit per-event lines
            "--append-system-prompt", f"{self.system_prompt}\n\nThe player asking is named {player_name}.",
            "--mcp-config", "/app/claude-config/mcp.json",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]

        log.info("claude ask player=%s session=%s prompt_len=%d", player_name, session_id or "new", len(prompt))
        # CALLER_PLAYER lets the MCP tools enforce "this action is taken
        # on behalf of THIS player" without trusting an LLM-supplied arg.
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env={**os.environ, "CALLER_PLAYER": player_name},
        )
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception as e:
            log.warning("failed to send prompt to claude stdin: %s", e)

        deadline = time.time() + self.timeout
        final_text = ""
        new_session: str | None = None
        had_error = False

        try:
            for raw in proc.stdout:
                if time.time() > deadline:
                    log.error("claude timed out for %s", player_name)
                    proc.kill()
                    return "(timed out — try again with a shorter question)"
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "assistant" and on_progress:
                    msg = event.get("message", {}) or {}
                    for block in msg.get("content", []) or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            try:
                                on_progress(_progress_label(block))
                            except Exception as e:
                                log.warning("on_progress callback failed: %s", e)
                elif etype == "result":
                    if event.get("is_error"):
                        had_error = True
                        log.error("claude result error: subtype=%s",
                                  event.get("subtype"))
                    final_text = (event.get("result") or "").strip()
                    new_session = event.get("session_id")
                    break
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        if proc.returncode and not final_text:
            stderr_tail = ""
            try:
                stderr_tail = (proc.stderr.read() or "").strip()[:500]
            except Exception:
                pass
            log.error("claude exited %s: stderr=%s", proc.returncode, stderr_tail)
            if session_id and "session" in stderr_tail.lower():
                self.sessions.set(player_uuid, "")
            return "(sorry, I hit an error)"

        if new_session and new_session != session_id:
            self.sessions.set(player_uuid, new_session)

        if had_error and not final_text:
            return "(sorry, I hit an error)"
        return final_text or "(no reply)"


# ---------------------------------------------------------------------------
# Friendly progress labels for tool_use events. The mapping is intentionally
# soft — we surface "what is the assistant doing?" in human terms rather than
# leaking tool / RCON command names. Falls back to a generic label so a new
# tool we forgot to map still produces something coherent.
# ---------------------------------------------------------------------------
def _progress_label(block: dict) -> str:
    name = block.get("name") or ""
    inp = block.get("input") or {}
    short = name.rsplit("__", 1)[-1] if "__" in name else name

    if short == "run_query_command":
        cmd = (inp.get("command") or "").strip()
        starts = (
            ("claudemod query inventory", "checking your inventory"),
            ("claudemod query xp",        "checking your XP"),
            ("claudemod query stats",     "looking up your stats"),
            ("claudemod query recipes",   "looking up the recipe"),
            ("claudemod query trinkets",  "checking your trinkets"),
            ("claudemod query skills",    "checking your skill tree"),
            ("claudemod query quest",     "searching the quest book"),
            ("claudemod query find",      "scanning chests"),
            ("claudemod query nbt_keys",  "indexing your data"),
            ("claudemod mark",            "updating the map"),
            ("data get entity",           "checking entity data"),
            ("list",                      "checking who's online"),
            ("time query",                "checking the time"),
            ("scoreboard",                "reading the scoreboard"),
        )
        for prefix, label in starts:
            if cmd.startswith(prefix):
                return label
        return "querying server"
    if short == "teleport_caller_to_player":
        return "teleporting you"
    if short == "add_bluemap_marker":
        return "marking the map"
    if short == "record_feature_request":
        return "logging your feature request"
    if short == "WebSearch":
        q = (inp.get("query") or "").strip()
        return f"searching the web for {q[:30]}" if q else "searching the web"
    if short == "WebFetch":
        url = (inp.get("url") or "")
        host = url.split("/", 3)[2] if url.startswith("http") and "/" in url[8:] else url[:30]
        return f"fetching {host}" if host else "fetching"
    if short in ("Read", "Glob", "Grep"):
        return "reading docs"
    # Claude Code internal tools that show up in stream events but aren't
    # interesting to surface to the player as progress. Map them to a
    # generic "thinking" so the bossbar doesn't say "using ToolSearch".
    if short in ("ToolSearch",):
        return "thinking"
    return f"using {short}" if short else "thinking"


# ---------------------------------------------------------------------------
# tellraw broadcast — splits long responses across multiple chat lines so
# Minecraft's per-message budget doesn't truncate mid-word. Each chunk is
# parsed for a markdown-lite subset (**bold**, *italic*, `code`) and rendered
# as styled tellraw components.
# ---------------------------------------------------------------------------
def broadcast(rcon: RCON, who: str, text: str, max_chars: int) -> None:
    text = text.strip().replace("\r", "")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"

    # Split on whitespace into ~200-char chunks so a single long reply still
    # renders cleanly on default-width chat. tellraw itself can take longer
    # strings, but client-side wrap gets ugly past ~220.
    chunks = list(_chunk(text, 200))
    for i, chunk in enumerate(chunks):
        prefix = f"[Claude → {who}]" if i == 0 else "[…]"
        components = [{"text": prefix + " ", "color": "aqua", "bold": True}]
        components.extend(_markdown_to_components(chunk))
        rcon.send(f"tellraw @a {json.dumps(components)}")


# Markdown-lite → tellraw components.
#   **text**  → bold (white)
#   *text*    → italic (gray)
#   `text`    → highlighted (aqua) — good for item ids, commands, coords
# Anything else stays plain white. Unmatched delimiters render as literals.
# Nested formatting is intentionally not supported (keeps the parser tiny).
_MD_DELIMS = ("**", "`", "*")  # order matters: ** before *


def _markdown_to_components(text: str) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(text):
        # Try each delimiter at this position.
        matched = False
        for delim in _MD_DELIMS:
            if text.startswith(delim, i):
                end = text.find(delim, i + len(delim))
                if end == -1:
                    continue
                inner = text[i + len(delim):end]
                if delim == "**":
                    out.append({"text": inner, "color": "white", "bold": True})
                elif delim == "`":
                    out.append({"text": inner, "color": "aqua"})
                else:  # "*"
                    out.append({"text": inner, "color": "gray", "italic": True})
                i = end + len(delim)
                matched = True
                break
        if matched:
            continue
        # Plain run — read up to the next delimiter.
        next_pos = len(text)
        for delim in _MD_DELIMS:
            p = text.find(delim, i)
            if p != -1 and p < next_pos:
                next_pos = p
        if next_pos == i:
            # Shouldn't happen, but guard against infinite loop.
            next_pos = i + 1
        out.append({"text": text[i:next_pos], "color": "white"})
        i = next_pos
    return out


# ---------------------------------------------------------------------------
# Per-request bossbar — one updateable status indicator per active /claude
# request. Replaces the old in-chat "thinking…" line so progress doesn't
# spam chat. Created when a request is dequeued, updated on every tool_use
# event, removed when the final reply lands. Uses vanilla `bossbar`, no mod
# changes required.
# ---------------------------------------------------------------------------
_BOSSBAR_NAME_RE = re.compile(r"[^a-z0-9_]")


def _bossbar_id(player: str) -> str:
    safe = _BOSSBAR_NAME_RE.sub("_", player.lower())
    return f"claudemod:claude_{safe}"


def bossbar_create(rcon: RCON, player: str, text: str) -> None:
    bid = _bossbar_id(player)
    name = json.dumps({"text": text, "color": "aqua"})
    # Defensive cleanup of any orphan from a previous request.
    try:
        rcon.send(f"bossbar remove {bid}")
    except Exception:
        pass
    rcon.send(f"bossbar add {bid} {name}")
    rcon.send(f"bossbar set {bid} color blue")
    rcon.send(f"bossbar set {bid} max 1")
    rcon.send(f"bossbar set {bid} value 1")
    rcon.send(f"bossbar set {bid} players {player}")


def bossbar_update(rcon: RCON, player: str, text: str) -> None:
    bid = _bossbar_id(player)
    name = json.dumps({"text": text, "color": "aqua"})
    rcon.send(f"bossbar set {bid} name {name}")


def bossbar_remove(rcon: RCON, player: str) -> None:
    bid = _bossbar_id(player)
    try:
        rcon.send(f"bossbar remove {bid}")
    except Exception:
        pass


def _chunk(text: str, n: int) -> Iterable[str]:
    if len(text) <= n:
        yield text
        return
    words = text.split(" ")
    buf = ""
    for w in words:
        if buf and len(buf) + 1 + len(w) > n:
            yield buf
            buf = w
        else:
            buf = f"{buf} {w}".strip()
    if buf:
        yield buf


# ---------------------------------------------------------------------------
# Log stream — finds the Minecraft pod by label and follows its stdout.
# Reconnects on EOF / pod replacement.
# ---------------------------------------------------------------------------
def stream_minecraft_logs(cfg: Config) -> Iterable[str]:
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    while True:
        try:
            pods = v1.list_namespaced_pod(cfg.mc_namespace, label_selector=cfg.mc_pod_selector).items
            running = [p for p in pods if p.status.phase == "Running"]
            if not running:
                log.info("no running minecraft pod, sleeping")
                time.sleep(5)
                continue
            pod = running[0]
            log.info("attaching to log stream of %s", pod.metadata.name)
            stream = v1.read_namespaced_pod_log(
                name=pod.metadata.name,
                namespace=cfg.mc_namespace,
                container=cfg.mc_pod_container,
                follow=True,
                _preload_content=False,
                tail_lines=0,
            )
            for raw in stream.stream(decode_content=False):
                for line in raw.decode("utf-8", errors="replace").splitlines():
                    yield line
            log.warning("log stream ended; reconnecting")
        except ApiException as e:
            log.warning("k8s api error: %s; sleeping 5s", e)
            time.sleep(5)
        except Exception as e:
            log.exception("log stream crashed: %s; sleeping 5s", e)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Log parsing.
#
# UUID announcement on connect (used to learn name->uuid for sessions):
#   [HH:MM:SS] [Server thread/INFO]: UUID of player Bob is 12345-67-...
#
# Slash command from the sibling claude-mod (sees /claude <prompt> via
# Brigadier, prints this exact line via SLF4J on the Server thread):
#   [HH:MM:SS] [Server thread/INFO] [claudemod/]: [ClaudeRequest] Bob: prompt
# We deliberately don't match public chat — the chat path was removed when
# we moved to a slash command, since dcintegration relays public chat to
# Discord and we wanted to stop spamming the channel.
# ---------------------------------------------------------------------------
UUID_RE = re.compile(r"\[Server thread/INFO\]:?\s*UUID of player (\S+) is ([0-9a-f-]{36})")
COMMAND_RE = re.compile(r"\[ClaudeRequest\]\s+(\S+):\s+(.+)$")


def main() -> None:
    rcon = RCON(CFG.rcon_host, CFG.rcon_port, CFG.rcon_password)
    whitelist = Whitelist(rcon)
    limiter = RateLimiter(CFG.rate_limit_requests, CFG.rate_limit_window)
    sessions = SessionStore(CFG.state_dir / "sessions.json")
    claude = Claude(sessions, CFG.system_prompt, CFG.model, CFG.request_timeout)

    setup_feedback_repo()

    # name (lowercase) -> uuid, learned from connect lines. Names without a
    # known UUID still work — we fall back to the name as the session key.
    name_to_uuid: dict[str, str] = {}

    work_q: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=32)

    def worker() -> None:
        while True:
            player, prompt = work_q.get()
            bossbar_active = False
            try:
                try:
                    bossbar_create(rcon, player, "Claude is thinking…")
                    bossbar_active = True
                except Exception as e:
                    log.warning("bossbar create failed for %s: %s", player, e)

                def on_progress(label: str) -> None:
                    if not bossbar_active:
                        return
                    try:
                        bossbar_update(rcon, player, f"Claude: {label}…")
                    except Exception as e:
                        log.warning("bossbar update failed for %s: %s", player, e)

                key = name_to_uuid.get(player.lower(), player.lower())
                reply = claude.ask(key, player, prompt, on_progress=on_progress)
                broadcast(rcon, player, reply, CFG.max_response_chars)
            except Exception as e:
                log.exception("worker error for %s: %s", player, e)
                try:
                    broadcast(rcon, player, "(sorry, something broke)", CFG.max_response_chars)
                except Exception:
                    pass
            finally:
                if bossbar_active:
                    try:
                        bossbar_remove(rcon, player)
                    except Exception:
                        pass

    threading.Thread(target=worker, daemon=True, name="claude-worker").start()

    log.info("bridge ready; trigger=/claude whitelist=%s", CFG.enforce_whitelist)

    for line in stream_minecraft_logs(CFG):
        m = UUID_RE.search(line)
        if m:
            name, uuid = m.group(1), m.group(2)
            name_to_uuid[name.lower()] = uuid
            log.debug("learned UUID for %s = %s", name, uuid)
            continue

        m = COMMAND_RE.search(line)
        if not m:
            continue
        player, prompt = m.group(1), m.group(2).strip()
        if not prompt:
            continue

        if CFG.enforce_whitelist and not whitelist.contains(player):
            log.info("rejecting non-whitelisted player %s", player)
            continue
        if len(prompt) > CFG.max_prompt_chars:
            try:
                broadcast(rcon, player, f"(prompt too long; max {CFG.max_prompt_chars} chars)", CFG.max_response_chars)
            except Exception:
                pass
            continue
        if not limiter.allow(player.lower()):
            log.info("rate limit hit for %s", player)
            try:
                broadcast(rcon, player, "(slow down — too many requests)", CFG.max_response_chars)
            except Exception:
                pass
            continue

        try:
            work_q.put_nowait((player, prompt))
        except queue.Full:
            log.warning("work queue full; dropping prompt from %s", player)
            try:
                broadcast(rcon, player, "(busy — try again in a sec)", CFG.max_response_chars)
            except Exception:
                pass


def setup_feedback_repo() -> None:
    """
    Clone (or refresh) the feedback repo into STATE_DIR/feedback-repo. The MCP
    server commits/pushes from there. Auth uses an HTTPS PAT injected into the
    URL — kept off disk by setting it only in env when we run git.
    """
    target = CFG.state_dir / "feedback-repo"
    target.parent.mkdir(parents=True, exist_ok=True)

    if not CFG.github_token:
        log.warning("GITHUB_TOKEN unset — feature requests will fall back to JSONL only")
        return

    # Embed the token in the remote URL so subsequent pushes don't need an
    # askpass helper. The URL only ever lives in the local .git/config of the
    # PVC, which is not world-readable.
    auth_url = CFG.feedback_repo_url.replace(
        "https://", f"https://x-access-token:{CFG.github_token}@", 1,
    )

    if (target / ".git").exists():
        log.info("refreshing existing feedback repo at %s", target)
        _git(target, "remote", "set-url", "origin", auth_url)
        _git(target, "fetch", "origin", CFG.feedback_branch)
        _git(target, "checkout", CFG.feedback_branch)
        _git(target, "reset", "--hard", f"origin/{CFG.feedback_branch}")
    else:
        log.info("cloning feedback repo to %s", target)
        if target.exists():
            shutil.rmtree(target)
        subprocess.run(
            ["git", "clone", "--branch", CFG.feedback_branch, "--depth", "1", auth_url, str(target)],
            check=True, capture_output=True, text=True,
        )

    _git(target, "config", "user.name", CFG.feedback_git_user)
    _git(target, "config", "user.email", CFG.feedback_git_email)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _shutdown(signum, _frame):
    log.info("received signal %d, exiting", signum)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    main()
