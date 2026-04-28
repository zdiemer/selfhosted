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
# Generic read-only RCON query — Claude composes commands dynamically rather
# than us baking a tool per data type. The allowlist below scopes this to
# verbs that can't change game state. Mutations (give, op, kill, gamemode,
# data merge/modify/remove, time set, xp add, summon, fill, setblock, etc.)
# are not on the list and get rejected before reaching RCON.
# ---------------------------------------------------------------------------
SAFE_QUERY_PATTERNS = [
    re.compile(p) for p in [
        r"^data\s+get\b",                             # NBT reads (inventory, pos, effects)
        r"^xp\s+query\b",                             # xp level / points
        r"^time\s+query\b",                           # in-game time
        r"^scoreboard\s+(objectives|players)\s+(list|get)\b",
        r"^forceload\s+query\b",
        r"^list(\s+uuids)?\s*$",                      # online players (+ uuids)
        r"^whitelist\s+list\s*$",
        r"^team\s+list\b",
        r"^tag\s+\S+\s+list\b",
        r"^seed\s*$",
        r"^difficulty\s*$",                           # bare = query
        r"^attribute\s+\S+\s+\S+\s+get\b",
        r"^gamerule\s+\S+\s*$",                       # gamerule <rule> = query
        r"^claudemod\s+query\b",                      # mod's structured queries
        r"^claudemod\s+mark\s+list\s*$",              # bluemap marker list (read)
        r"^claudemod\s+mark\s+remove\s+\S+\s*$",      # bluemap marker remove (small mutation, scoped)
    ]
]


@mcp.tool()
def run_query_command(command: str) -> str:
    """
    Execute a read-only Minecraft server query and return the raw output.

    Use this to look up live game state when answering player questions.
    Compose queries dynamically based on what's needed.

    Allowlist (read-only verbs only):
      - data get entity <player> <path>          # NBT: inventory, pos, effects, attrs
      - xp query <player> <points|levels>        # XP state
      - time query daytime                       # in-game time
      - list / list uuids                        # online players
      - whitelist list                           # whitelisted accounts
      - team list [<team>]                       # team membership
      - tag <player> list                        # tags on player
      - scoreboard players|objectives list|get   # scoreboard reads
      - attribute <target> <attr> get            # attribute value
      - gamerule <rule>                          # current rule value
      - difficulty                               # current difficulty
      - seed                                     # world seed
      - forceload query                          # forced chunks
      - claudemod query inventory <player>       # full inventory JSON
      - claudemod query xp <player>              # level + progress + total
      - claudemod query stats <player> [type]    # vanilla stats (optionally filtered)
      - claudemod query recipes makes <item>     # recipes producing this item
      - claudemod query recipes uses  <item>     # recipes using this item

    Mutation commands (give, op, gamemode, kill, kick, ban, setblock, fill,
    summon, weather, time set, xp add/set, data merge/modify/remove, etc.)
    are blocked. If you need teleport, use teleport_caller_to_player.

    Args:
        command: The command WITHOUT a leading slash (e.g.
                 "data get entity StarFoxA Inventory" or
                 "claudemod query xp StarFoxA").

    Returns:
        Raw RCON output. For claudemod query commands the output is JSON;
        for vanilla reads it's the standard SNBT / text format. Parse as
        needed.
    """
    cleaned = (command or "").strip().lstrip("/")
    if not cleaned:
        return "(rejected: empty command)"
    if not any(p.match(cleaned) for p in SAFE_QUERY_PATTERNS):
        return f"(rejected: '{cleaned.split()[0]}' or its sub-verb is not on the read-only allowlist)"
    try:
        return _rcon(cleaned)
    except Exception as e:
        print(f"run_query_command rcon failed: {e}", file=sys.stderr)
        return f"(rcon failed: {e})"


# ---------------------------------------------------------------------------
# BlueMap marker creation — caller-only, position auto-resolved from the
# requesting player's current entity data. Marker management beyond "add"
# (list / remove) goes through run_query_command since those are essentially
# read-only or trivially-scoped mutations.
# ---------------------------------------------------------------------------
_POS_RE = re.compile(r"\[(-?\d+\.?\d*)d?,\s*(-?\d+\.?\d*)d?,\s*(-?\d+\.?\d*)d?\]")
_DIM_RE = re.compile(r'"(?P<dim>[a-z0-9_\-.]+:[a-z0-9_\-./]+)"')
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")


@mcp.tool()
def add_bluemap_marker(name: str, label: str = "") -> str:
    """
    Add a point-of-interest marker to the public BlueMap web view at the
    requesting player's current position.

    Use this when a player wants to remember a location ("mark this as the
    wizard tower", "save this spot as my base", "remember here as
    starting_island"). The marker shows up as a labeled pin on the map
    that everyone can see.

    The marker is anchored to the player's CURRENT coordinates and
    dimension at the moment the command runs. The asking player is
    recorded as the marker's author for accountability.

    Args:
        name: Internal unique identifier (1-32 chars, letters/digits/-/_,
              no spaces). Used to remove the marker later. Pick something
              short and stable like "wizard_tower" or "bobs_base".
        label: Display label shown on the map. Falls back to `name` if
               empty. May contain spaces and most punctuation.

    Returns:
        Human-readable confirmation including the coords, or an error
        string starting with "(". Surface this back to the player so
        they know it landed.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return "(error: caller name not available; refusing to add marker)"
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return "(error: marker name must be 1-32 chars, letters/digits/-/_ only, no spaces)"
    label = (label or name).strip()
    # Strip newlines / control chars from the label just to be safe — it
    # ends up rendered in BlueMap's web UI.
    label = re.sub(r"[\r\n\t]+", " ", label)[:120]

    try:
        pos_out = _rcon(f"data get entity {caller} Pos")
    except Exception as e:
        print(f"add_bluemap_marker: pos lookup failed: {e}", file=sys.stderr)
        return f"({caller} doesn't appear to be online — try again after rejoining)"

    pm = _POS_RE.search(pos_out)
    if not pm:
        return f"(couldn't parse position; raw: {pos_out[:200]})"
    x, y, z = float(pm.group(1)), float(pm.group(2)), float(pm.group(3))

    # Resolve the player's current dimension. Falls back to overworld if
    # the response shape isn't what we expect (e.g. modpack formatting tweak).
    dim = "minecraft:overworld"
    try:
        dim_out = _rcon(f"data get entity {caller} Dimension")
        dm = _DIM_RE.search(dim_out)
        if dm:
            dim = dm.group("dim")
    except Exception:
        pass

    # Brigadier label argument is greedyString — passing it last is fine.
    cmd = f"claudemod mark add {name} {dim} {x:.2f} {y:.2f} {z:.2f} {caller} {label}"
    try:
        return _rcon(cmd)
    except Exception as e:
        print(f"add_bluemap_marker: rcon failed: {e}", file=sys.stderr)
        return f"(failed to add marker: {e})"


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
    # REMOVE strips Minecraft §-codes and ANSI escapes from the response so
    # Claude sees clean text (RCON adds reset codes around multi-line output).
    c = RCONClient(host, port, format_method=RCONClient.REMOVE)
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
