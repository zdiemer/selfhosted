"""
Stdio MCP server bundling the bridge's tools.

Tools:
  - record_feature_request: append to FEEDBACK.md, commit, push.
  - teleport_caller_to_player: teleport the requesting player to another
    online player (caller identity comes from $CALLER_PLAYER env so Claude
    can't spoof it).

Claude Code spawns one of these per request. The tools open short-lived
RCON connections directly — independent of the bridge's RCON pool — so
the MCP server stays free of bridge imports.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mctools import RCONClient

STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
REPO_DIR = STATE_DIR / "feedback-repo"
AUDIT_LOG = STATE_DIR / "feature-requests.jsonl"
FEEDBACK_FILE = os.environ.get("FEEDBACK_FILE", "FEEDBACK.md")
BRANCH = os.environ.get("FEEDBACK_BRANCH", "main")

mcp = FastMCP("claude-bridge-feedback")


# ---------------------------------------------------------------------------
# Feature requests
# ---------------------------------------------------------------------------
@mcp.tool()
def record_feature_request(player_name: str, summary: str, verbatim: str = "") -> str:
    """
    Record a Minecraft player's feature request.

    Call this whenever a player expresses a wish for new server behavior,
    a new mod, a config change, or anything else they'd like to see added
    to the server. Do NOT call this for general questions.

    Args:
        player_name: The Minecraft username of the player making the request.
        summary: A one-line summary of the request (≤120 chars). This is what
                 will appear in FEEDBACK.md, so make it self-contained.
        verbatim: The original wording, in quotes, so the maintainer has the
                  raw context. Optional but encouraged.

    Returns:
        Human-readable confirmation. Surface this back to the player so they
        know the request was logged.
    """
    summary = summary.strip().replace("\n", " ")[:200]
    verbatim = verbatim.strip().replace("\n", " ")[:300]
    player_name = player_name.strip()[:32]

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "player": player_name,
            "summary": summary,
            "verbatim": verbatim,
        }) + "\n")

    try:
        _commit_to_feedback(timestamp, player_name, summary, verbatim)
        return f"Logged your request and pushed it to FEEDBACK.md. Thanks, {player_name}."
    except Exception as e:
        print(f"git push failed (request still in audit log): {e}", file=sys.stderr)
        return f"Logged your request locally. Thanks, {player_name}."


# ---------------------------------------------------------------------------
# Teleport — caller-only. The CALLER_PLAYER env var is set by the bridge per
# request and is the only acceptable identity; an LLM-supplied caller arg
# would let players ask Claude to teleport someone else.
# ---------------------------------------------------------------------------
@mcp.tool()
def teleport_caller_to_player(target_player: str) -> str:
    """
    Teleport the requesting player to another online player.

    Use this when the player asks to be moved to someone (e.g. "tp me to
    Bob", "take me to Indelmaen", "I want to go where Charlie is"). The
    teleport always moves the requesting player — never the target, and
    never anyone else. If the player asks you to move someone else,
    politely decline.

    Args:
        target_player: The Minecraft username of the player to teleport TO.

    Returns:
        Human-readable confirmation. Surface this back to the player.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return "(error: caller name not available; refusing teleport)"
    target = (target_player or "").strip()
    if not target:
        return "(error: no target player specified)"
    if caller.lower() == target.lower():
        return f"You're already where you are, {caller}."

    try:
        online = _online_players()
    except Exception as e:
        print(f"online lookup failed: {e}", file=sys.stderr)
        return "(couldn't check who's online; try again in a moment)"

    online_lc = {p.lower(): p for p in online}
    if caller.lower() not in online_lc:
        return f"(error: {caller} doesn't appear to be online — refusing teleport)"
    if target.lower() not in online_lc:
        return f"{target} is not online right now."

    canonical = online_lc[target.lower()]
    try:
        _rcon(f"tp {caller} {canonical}")
    except Exception as e:
        print(f"tp failed: {e}", file=sys.stderr)
        return "(teleport failed; the server rejected the command)"
    return f"Teleported {caller} → {canonical}."


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _online_players() -> list[str]:
    """Vanilla `list` output: 'There are 2 of a max of 10 players online: A, B'."""
    out = _rcon("list")
    m = re.search(r"online:\s*(.+)", out)
    if not m:
        return []
    return [n.strip() for n in m.group(1).split(",") if n.strip()]


def _rcon(command: str) -> str:
    host = os.environ["RCON_HOST"]
    port = int(os.environ.get("RCON_PORT", "25575"))
    password = os.environ["RCON_PASSWORD"]
    c = RCONClient(host, port, format_method=RCONClient.RAW)
    if not c.login(password):
        raise RuntimeError("RCON login failed")
    try:
        return c.command(command)
    finally:
        try:
            c.stop()
        except Exception:
            pass


def _commit_to_feedback(timestamp: str, player: str, summary: str, verbatim: str) -> None:
    if not (REPO_DIR / ".git").exists():
        raise RuntimeError(f"feedback repo not initialized at {REPO_DIR}")

    _git("fetch", "origin", BRANCH)
    _git("reset", "--hard", f"origin/{BRANCH}")

    target = REPO_DIR / FEEDBACK_FILE
    if not target.exists():
        target.write_text(
            "# Feedback\n\n"
            "Player feature requests captured by the in-game `!claude` bridge.\n"
            "Each entry is appended automatically — feel free to triage / "
            "delete by hand.\n\n"
        )
    line = f"- **{timestamp}** _{player}_ — {summary}"
    if verbatim:
        line += f' (player said: "{verbatim}")'
    line += "\n"
    with target.open("a") as f:
        f.write(line)

    _git("add", FEEDBACK_FILE)
    _git("commit", "-m", f"feedback: {player} — {summary[:60]}")
    _git("push", "origin", BRANCH)


def _git(*args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


if __name__ == "__main__":
    mcp.run()
