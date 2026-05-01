"""
Stdio MCP server bundling the bridge's tools.

Tools (read-only and writes; caller identity comes from $CALLER_PLAYER
env so Claude can't spoof it):
  - record_feature_request: append to FEEDBACK.md, commit, push.
  - teleport_caller_to_player / teleport_caller_home / teleport_caller_back
  - run_query_command: gated RCON dispatcher (allowlist for non-admins).
  - add_bluemap_marker
  - read_container / read_inventory / read_backpack: dump container state.
  - preview_container_reorg / preview_inventory_reorg / preview_backpack_reorg:
    validate a proposed layout client-side, return a txn_id.
  - commit_container_reorg: apply a previewed layout via the mod's chunked
    write protocol; snapshots the pre-state to PVC for undo.
  - undo_container_reorg: restore from a snapshot.

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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mctools import RCONClient

STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
REPO_DIR = STATE_DIR / "feedback-repo"
AUDIT_LOG = STATE_DIR / "feature-requests.jsonl"
FEEDBACK_FILE = os.environ.get("FEEDBACK_FILE", "FEEDBACK.md")
BRANCH = os.environ.get("FEEDBACK_BRANCH", "main")
SNAPSHOT_DIR = STATE_DIR / "container-snapshots"
SNAPSHOTS_PER_PLAYER = 5
PREVIEW_TTL_S = 60
# Per-player most-recent pre-tp position, persisted so `teleport_caller_back`
# survives bridge restarts. Single entry per player — toggle semantics: each
# tp (including a `back`) replaces the snapshot with the position the player
# is leaving, so a second `back` returns them forward again.
TP_SNAPSHOT_DIR = STATE_DIR / "tp-snapshots"

# Source RCON request payloads top out around 1413 bytes; framing overhead
# for a chunked write command ("claudemod write txn_slot_part <txn_id>
# <slot> <chunk_idx> <part>") is roughly 60 chars, so we leave generous
# headroom rather than maximizing throughput. 600 chars per chunk × ~13
# bytes/byte-of-NBT-base64 still moves several KB of NBT per slot in a
# few RCON round trips.
WRITE_INLINE_MAX = 800    # full `<count>:<base64>` length budget for one-shot
WRITE_CHUNK_SIZE = 600    # per-fragment base64 size for chunked writes
# A single item with NBT base64 above this threshold is reported up-front
# at preview time as "unmovable" — the LLM must keep it in its current slot
# rather than relocating it. Most items fit easily; the cap protects us
# against mods that store kilobytes of metadata on a single item (e.g.
# patchouli guidebooks, written books with many pages, modded reagent
# bags). Set generously — even a 100-page written book is comfortably
# below this — but bounded so a multi-MB outlier doesn't lock the bridge.
ITEM_MOVE_NBT_MAX = 200_000

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
# Each successful tp persists the pre-tp pos+dim to a per-player file so
# `teleport_caller_back` can reverse the move (toggle: a second `back` returns
# the player forward again). Single most-recent entry per player.
# ---------------------------------------------------------------------------
def _capture_caller_pos_dim(caller: str) -> dict[str, Any] | None:
    """Resolve the caller's current pos + dim. Returns None on any failure."""
    try:
        pos_out = _rcon(f"data get entity {caller} Pos")
    except Exception as e:
        print(f"_capture_caller_pos_dim: pos lookup failed: {e}", file=sys.stderr)
        return None
    pm = _POS_RE.search(pos_out or "")
    if not pm:
        return None
    x, y, z = float(pm.group(1)), float(pm.group(2)), float(pm.group(3))
    dim = "minecraft:overworld"
    try:
        dim_out = _rcon(f"data get entity {caller} Dimension")
        dm = _DIM_RE.search(dim_out or "")
        if dm:
            dim = dm.group("dim")
    except Exception:
        pass
    return {"x": x, "y": y, "z": z, "dim": dim}


def _tp_snapshot_path(caller: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", caller)
    return TP_SNAPSHOT_DIR / safe / "last.json"


def _save_tp_snapshot(caller: str, snap: dict[str, Any], trigger: str) -> None:
    """Atomically persist the most-recent pre-tp position for `caller`."""
    path = _tp_snapshot_path(caller)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "trigger": trigger,
        "x": snap["x"], "y": snap["y"], "z": snap["z"],
        "dim": snap["dim"],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _load_tp_snapshot(caller: str) -> dict[str, Any] | None:
    path = _tp_snapshot_path(caller)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"_load_tp_snapshot: read failed: {e}", file=sys.stderr)
        return None


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
    pre_snap = _capture_caller_pos_dim(caller)
    try:
        _rcon(f"tp {caller} {canonical}")
    except Exception as e:
        print(f"tp failed: {e}", file=sys.stderr)
        return "(teleport failed; the server rejected the command)"
    if pre_snap is not None:
        try:
            _save_tp_snapshot(caller, pre_snap, trigger="to_player")
        except Exception as e:
            print(f"tp snapshot save failed: {e}", file=sys.stderr)
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
    Execute a Minecraft server command via RCON and return the raw output.

    There are two modes, gated by the asking player's admin status (set by
    the bridge from a configured allowlist of player names):

    NON-ADMIN callers (default): only read-only verbs run. Allowlist:
      - data get entity <player> <path>          # NBT reads
      - xp query <player> <points|levels>
      - time query daytime
      - list / list uuids                        # online players
      - whitelist list, team list, tag <p> list  # other reads
      - scoreboard players|objectives list|get
      - attribute <target> <attr> get
      - gamerule <rule>                          # current value
      - difficulty / seed / forceload query
      - claudemod query ...                      # mod's structured queries
      - claudemod mark list / mark remove <name> # bluemap markers (small mut)

    ADMIN callers (CALLER_IS_ADMIN env == "true"): every constraint above
    is dropped — admins can run ANY RCON command, including mutations
    (op, deop, gamemode, give, kill, kick, ban, setblock, fill, summon,
    weather, time set, etc.). Use this responsibly:
      - Confirm DESTRUCTIVE actions with the player before running them
        ("Are you sure you want to ban X?", "This will kill all entities
        in a 100-block radius — proceed?").
      - For trivially reversible actions (op, gamemode, give, weather)
        just run it and report the result.
      - Don't volunteer admin-only capabilities to non-admin players;
        the tool's rejection message tells them they lack permission.

    Args:
        command: The command WITHOUT a leading slash (e.g.
                 "data get entity StarFoxA Inventory" for non-admins,
                 "op SomePlayer" for admins).

    Returns:
        Raw RCON output. For claudemod query commands the output is JSON;
        for vanilla reads/writes it's standard text. On a rejection, the
        return string starts with "(rejected:".
    """
    cleaned = (command or "").strip().lstrip("/")
    if not cleaned:
        return "(rejected: empty command)"
    is_admin = os.environ.get("CALLER_IS_ADMIN", "").lower() == "true"
    if not is_admin and not any(p.match(cleaned) for p in SAFE_QUERY_PATTERNS):
        return (f"(rejected: '{cleaned.split()[0]}' or its sub-verb is not on "
                "the read-only allowlist; this command requires admin)")
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
def teleport_caller_home() -> str:
    """
    Teleport the requesting player to their bed / respawn point.

    Use when the player asks to go home, return to base, "tp me home",
    "send me to bed", etc. Resolves the bed via the asking player's
    SpawnPointPosition + SpawnPointDimension, so it correctly handles
    cross-dimensional respawns (e.g. nether respawn anchor).

    Args:
        (none — caller identity comes from CALLER_PLAYER env)

    Returns:
        Confirmation including coords + dimension on success, or an
        error message if the player has no spawn set yet (they need
        to sleep in a bed / set a respawn anchor first).
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return "(error: caller name not available; refusing teleport)"
    pre_snap = _capture_caller_pos_dim(caller)
    try:
        out = _rcon(f"claudemod home {caller}")
    except Exception as e:
        print(f"teleport_caller_home: rcon failed: {e}", file=sys.stderr)
        return f"(home teleport failed: {e})"
    # Mod returns JSON {ok, ...}; only persist the pre-tp snapshot if the
    # tp actually happened. On parse failure (mod responded with something
    # unexpected) skip the snapshot rather than guessing.
    if pre_snap is not None:
        try:
            parsed = json.loads(out or "{}")
            if parsed.get("ok"):
                _save_tp_snapshot(caller, pre_snap, trigger="home")
        except Exception as e:
            print(f"home snapshot decision failed: {e}", file=sys.stderr)
    return out


@mcp.tool()
def teleport_caller_back() -> str:
    """
    Reverse the requesting player's most recent teleport.

    Use when the player asks to undo a teleport ("tp me back", "go back to
    where I was", "undo that teleport", "send me back"). Restores the
    position + dimension the player was at right before their last
    teleport_caller_to_player or teleport_caller_home (or the previous
    teleport_caller_back — calling this twice in a row toggles the player
    forward and back).

    Returns an error if no recent teleport is recorded — e.g. the player
    has never used the bridge to teleport, or the bridge's state was
    wiped. There's no way to recover from that case; the player just has
    to use a regular tp / home command instead.

    Args:
        (none — caller identity comes from CALLER_PLAYER env)

    Returns:
        Confirmation including coords + dimension on success, or an error
        message starting with "(" on failure.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return "(error: caller name not available; refusing teleport)"
    snap = _load_tp_snapshot(caller)
    if snap is None:
        return ("(no recent teleport to reverse — I only remember the most "
                "recent tp, and there isn't one on file for you yet)")

    # Capture current pos BEFORE moving so a second `back` returns the
    # player forward. If the capture fails (player offline, etc.) we still
    # attempt the restore but won't persist a new snapshot — the failure
    # mode is "back works once, second back is a no-op", which is fine.
    cur_snap = _capture_caller_pos_dim(caller)

    x, y, z, dim = snap["x"], snap["y"], snap["z"], snap["dim"]
    # `execute in <dim> run tp <player> <x> <y> <z>` handles cross-dim
    # teleports; vanilla `tp` alone can't change dimension.
    cmd = f"execute in {dim} run tp {caller} {x:.2f} {y:.2f} {z:.2f}"
    try:
        _rcon(cmd)
    except Exception as e:
        print(f"teleport_caller_back: rcon failed: {e}", file=sys.stderr)
        return f"(teleport failed: {e})"

    if cur_snap is not None:
        try:
            _save_tp_snapshot(caller, cur_snap, trigger="back")
        except Exception as e:
            print(f"back snapshot save failed: {e}", file=sys.stderr)

    short_dim = dim.split(":", 1)[1] if ":" in dim else dim
    return f"Sent {caller} back to {x:.0f}, {y:.0f}, {z:.0f} in {short_dim}."


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


# ---------------------------------------------------------------------------
# Container-write protocol — chunked RCON conversation with claude-mod's
# /claudemod write subtree. The mod is authoritative for safety
# (distance, hopper detection, viewer guard, item conservation, TOCTOU
# hash); the bridge validates layouts client-side, persists snapshots
# for undo, and stitches the chunked read/write flow into single MCP
# tool calls.
# ---------------------------------------------------------------------------

# In-process pending-preview map. Bridge restart drops these; on commit
# the system prompt instructs Claude to re-preview if `unknown_txn`.
PREVIEW_STATE: dict[str, dict[str, Any]] = {}


def _gc_previews() -> None:
    now = time.time()
    expired = [k for k, v in PREVIEW_STATE.items() if v["expires_at"] < now]
    for k in expired:
        PREVIEW_STATE.pop(k, None)


def _mod_call(*args: str) -> dict[str, Any]:
    """Send a /claudemod write … sub-command and parse the JSON response."""
    cmd = " ".join(("claudemod", "write", *args))
    out = _rcon(cmd)
    out = (out or "").strip()
    if not out:
        return {"ok": False, "error": "empty_response", "detail": "no output from mod"}
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "error": "bad_response", "detail": out[:300]}


def _do_read(kind: str, *, caller: str, dim: str | None = None,
             x: int | None = None, y: int | None = None, z: int | None = None
             ) -> dict[str, Any]:
    """Run the chunked read protocol; return {ok, kind, half, slots:[...], contents_hash}."""
    if kind == "container":
        opened = _mod_call("read_open", "container", caller, dim, str(x), str(y), str(z))
    elif kind == "inventory":
        opened = _mod_call("read_open", "inventory", caller)
    elif kind == "backpack_equipped":
        opened = _mod_call("read_open", "backpack_equipped", caller)
    elif kind == "backpack_world":
        opened = _mod_call("read_open", "backpack_world", caller, dim, str(x), str(y), str(z))
    else:
        return {"ok": False, "error": "bad_kind", "detail": kind}
    if not opened.get("ok"):
        return opened
    txn_id = opened["txn_id"]
    total = int(opened["total_slots"])
    slots: list[dict[str, Any]] = []
    try:
        for i in range(total):
            r = _mod_call("read_slot", txn_id, str(i))
            if r.get("ok") is False:
                return r
            if r.get("empty"):
                continue
            entry = {"slot": i, "i": r["i"], "c": int(r["c"])}
            if r.get("n"):
                entry["n"] = r["n"]
            elif r.get("n_chunks"):
                # NBT exceeded the inline budget; assemble from chunks.
                n_chunks = int(r["n_chunks"])
                parts: list[str] = []
                for ci in range(n_chunks):
                    pr = _mod_call("read_slot_part", txn_id, str(i), str(ci))
                    if pr.get("ok") is False:
                        return pr
                    parts.append(pr.get("part", ""))
                entry["n"] = "".join(parts)
            slots.append(entry)
    finally:
        try:
            _mod_call("read_close", txn_id)
        except Exception:
            pass
    return {
        "ok": True,
        "kind": opened.get("kind", kind),
        "half": opened.get("half"),
        "total_slots": total,
        "contents_hash": opened["contents_hash"],
        "slots": slots,
    }


def _validate_layout(slots: list[dict[str, Any]], total: int,
                     new_layout: dict[str, list[int]] | list[Any]
                     ) -> dict[str, Any]:
    """
    Validate an LLM-supplied layout against the read result.

    `new_layout` accepts two shapes:
      dict[str|int, list[int]]: target_slot → list of source slot indices to
                                place there (multiple sources = merge)
      list[list[int] | None]:    target_slot index → sources (or None = empty)

    Each source slot must be referenced exactly once; merges require
    matching item ids. Returns {ok, target_stacks: {slot: {i, c, n}}}
    or {ok: false, error, detail}.
    """
    by_slot: dict[int, dict[str, Any]] = {s["slot"]: s for s in slots}
    unmovable: set[int] = {
        int(s["slot"]) for s in slots
        if isinstance(s.get("n"), str) and len(s["n"]) > ITEM_MOVE_NBT_MAX
    }

    # Normalize layout to dict[int, list[int]]
    norm: dict[int, list[int]] = {}
    if isinstance(new_layout, list):
        for i, entry in enumerate(new_layout):
            if entry is None or entry == [] or entry == {}:
                continue
            if isinstance(entry, dict) and "sources" in entry:
                norm[i] = [int(x) for x in entry["sources"]]
            elif isinstance(entry, list):
                norm[i] = [int(x) for x in entry]
            else:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"target {i}: expected list of source slots or null"}
    elif isinstance(new_layout, dict):
        for k, v in new_layout.items():
            try:
                ki = int(k)
            except Exception:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"target slot key '{k}' is not an int"}
            if v is None or v == []:
                continue
            if isinstance(v, dict) and "sources" in v:
                norm[ki] = [int(x) for x in v["sources"]]
            elif isinstance(v, list):
                norm[ki] = [int(x) for x in v]
            else:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"target {ki}: expected list of source slots"}
    else:
        return {"ok": False, "error": "bad_layout",
                "detail": "new_layout must be list or dict"}

    used: dict[int, int] = {}  # source slot → target where it was placed
    target_stacks: dict[int, dict[str, Any]] = {}
    for tgt, src_list in norm.items():
        if tgt < 0 or tgt >= total:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"target slot {tgt} out of range [0, {total})"}
        if not src_list:
            continue
        first = by_slot.get(src_list[0])
        if first is None:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"target {tgt}: source slot {src_list[0]} is empty"}
        item_id = first["i"]
        merged = 0
        for s in src_list:
            if s in used:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"source slot {s} referenced by target {used[s]} and target {tgt}"}
            stk = by_slot.get(s)
            if stk is None:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"target {tgt}: source slot {s} is empty"}
            if stk["i"] != item_id:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"target {tgt}: can't merge {item_id} with {stk['i']} from slot {s}"}
            if s in unmovable and s != tgt:
                return {"ok": False, "error": "unmovable_item",
                        "detail": (f"slot {s} holds an item with NBT > {ITEM_MOVE_NBT_MAX} "
                                   "bytes; it must stay in slot " + str(s))}
            used[s] = tgt
            merged += int(stk["c"])
        # The mod ItemStack max stack size is item-dependent; we conservatively
        # check 64 here. (Some items cap at 16 or 1 — the mod will reject if
        # we exceed during commit; this is just a friendlier early rejection.)
        if merged > 64:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"target {tgt}: merged count {merged} exceeds 64 (vanilla cap)"}
        target_stacks[tgt] = {
            "i": item_id,
            "c": merged,
            "n": first.get("n"),  # NBT from first source — same stripped identity for all
        }

    # Conservation: every non-empty source slot must be placed.
    all_sources = {s["slot"] for s in slots}
    unplaced = all_sources - set(used.keys())
    if unplaced:
        return {"ok": False, "error": "bad_layout",
                "detail": f"sources not placed (would be lost): {sorted(unplaced)}"}

    return {"ok": True, "target_stacks": target_stacks,
            "unmovable": sorted(unmovable)}


def _render_diff(before: list[dict[str, Any]], after: dict[int, dict[str, Any]],
                 total: int) -> str:
    """Human-readable preview of slot-by-slot changes."""
    by_before = {s["slot"]: s for s in before}
    lines: list[str] = []
    changed = 0
    for i in range(total):
        b = by_before.get(i)
        a = after.get(i)
        if b is None and a is None:
            continue
        b_str = "·" if b is None else f"{_short_id(b['i'])}×{b['c']}"
        a_str = "·" if a is None else f"{_short_id(a['i'])}×{a['c']}"
        if b_str == a_str:
            continue
        lines.append(f"  slot {i:>2}: {b_str:<24} → {a_str}")
        changed += 1
    if changed == 0:
        return "(no slot changes — layout is identical)"
    return f"{changed} slot(s) change:\n" + "\n".join(lines)


def _short_id(item_id: str) -> str:
    return item_id.split(":", 1)[1] if ":" in item_id else item_id


def _save_snapshot(state: dict[str, Any], pre_hash: str) -> str:
    """Persist a snapshot of the pre-commit contents; trim to ring-buffer cap."""
    caller = state["caller"]
    snap_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    pdir = SNAPSHOT_DIR / re.sub(r"[^A-Za-z0-9_\-]", "_", caller)
    pdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_id": snap_id,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "kind": state["kind"],
        "dim": state.get("dim"),
        "x": state.get("x"), "y": state.get("y"), "z": state.get("z"),
        "backpack_mode": state.get("backpack_mode"),
        "total_slots": state["total_slots"],
        "pre_commit_contents_hash": pre_hash,
        "snapshot_contents": state["snapshot_contents"],
    }
    tmp = pdir / f".{snap_id}.tmp"
    final = pdir / f"{snap_id}.json"
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, final)
    # Trim ring buffer.
    files = sorted([p for p in pdir.iterdir() if p.suffix == ".json"], key=lambda p: p.name)
    while len(files) > SNAPSHOTS_PER_PLAYER:
        try: files[0].unlink()
        except Exception: pass
        files = files[1:]
    return snap_id


def _do_commit(state: dict[str, Any]) -> dict[str, Any]:
    """Execute the chunked write protocol with the layout from `state`."""
    caller = state["caller"]
    kind = state["kind"]
    target_stacks: dict[int, dict[str, Any]] = state["target_stacks"]
    # 1. Open the mod-side write txn.
    args = ["txn_open", kind, caller]
    if kind in ("container", "backpack_world"):
        args += [state["dim"], str(state["x"]), str(state["y"]), str(state["z"])]
    args.append(state["contents_hash"])
    # Forward the mode literally — the mod accepts "restore" (skip hopper +
    # conservation, used by undo) and "multi" (skip per-target conservation
    # only, used by cross-container reorgs where conservation is enforced
    # across the union of targets at the bridge).
    mode = state.get("mode")
    if mode in ("restore", "multi"):
        args.append(mode)
    opened = _mod_call(*args)
    if not opened.get("ok"):
        return opened
    txn_id = opened["txn_id"]
    try:
        # 2. Stream non-empty target slots. Single-shot when payload fits
        #    in one RCON request, otherwise chunk via txn_slot_part /
        #    txn_slot_finish to handle heavy modded NBT (Tiered + spell
        #    engine + enchants etc.).
        for slot, stk in sorted(target_stacks.items()):
            n = stk.get("n")
            if not n:
                return {"ok": False, "error": "missing_nbt",
                        "detail": f"slot {slot}: read result didn't include NBT"}
            wire = f"{stk['c']}:{n}"
            if len(wire) <= WRITE_INLINE_MAX:
                r = _mod_call("txn_slot", txn_id, str(slot), wire)
                if not r.get("ok"):
                    try: _mod_call("txn_abort", txn_id)
                    except Exception: pass
                    return r
                continue
            # Chunked path: split the base64 (NOT the count prefix) and
            # send each chunk via txn_slot_part, then finalize with the
            # count via txn_slot_finish.
            for ci in range(0, len(n), WRITE_CHUNK_SIZE):
                part = n[ci:ci + WRITE_CHUNK_SIZE]
                idx = ci // WRITE_CHUNK_SIZE
                r = _mod_call("txn_slot_part", txn_id, str(slot), str(idx), part)
                if not r.get("ok"):
                    try: _mod_call("txn_abort", txn_id)
                    except Exception: pass
                    return r
            r = _mod_call("txn_slot_finish", txn_id, str(slot), str(stk["c"]))
            if not r.get("ok"):
                try: _mod_call("txn_abort", txn_id)
                except Exception: pass
                return r
        # 3. Commit.
        committed = _mod_call("txn_commit", txn_id)
        return committed
    except Exception as e:
        try: _mod_call("txn_abort", txn_id)
        except Exception: pass
        return {"ok": False, "error": "commit_exception", "detail": str(e)}


# ----- public MCP tools ----------------------------------------------------

@mcp.tool()
def read_container(dim: str, x: int, y: int, z: int) -> str:
    """
    Read a chest / barrel / shulker box's contents at the given coordinates.

    Use this when the player asks about (or asks you to act on) a specific
    container's contents and you have its coordinates. Returns the full
    slot inventory as JSON, including a `contents_hash` you should pass
    along to `preview_container_reorg` so the mod can detect tampering
    between read and commit.

    Distance gate: the caller must be within 8 blocks of the target.
    Reject if any hopper, dropper, or hopper minecart is feeding into or
    pulling from this container — those are race-condition risks at
    write time and are reported here too.

    Args:
        dim: Dimension id (e.g. "minecraft:overworld").
        x, y, z: Block coordinates of the chest/barrel/shulker.

    Returns:
        JSON string with {kind, half, total_slots, contents_hash,
        slots:[{slot, i, c, n}]} or {error, detail}.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return json.dumps({"error": "no_caller", "detail": "CALLER_PLAYER unset"})
    return json.dumps(_do_read("container", caller=caller, dim=dim, x=x, y=y, z=z))


@mcp.tool()
def read_inventory() -> str:
    """
    Read the requesting player's main inventory (slots 9–35 only — hotbar,
    armor, and off-hand are NOT exposed).

    Use this when the player asks you to organize, sort, or look at their
    own inventory. The slot indices in the response are 0–26 (dense),
    mapping to vanilla slots 9–35 internally — the LLM never needs to
    know the raw mapping.

    Returns:
        JSON string with {kind:"inventory", total_slots:27, contents_hash,
        slots:[{slot, i, c, n}]} or {error, detail}.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return json.dumps({"error": "no_caller", "detail": "CALLER_PLAYER unset"})
    return json.dumps(_do_read("inventory", caller=caller))


@mcp.tool()
def read_backpack(mode: str, dim: str = "", x: int = 0, y: int = 0, z: int = 0) -> str:
    """
    Read a Travelers Backpack — either equipped on the caller, or placed
    in the world as a block.

    Only the MAIN STORAGE slots are exposed. Crafting grid, tool slots,
    and fluid tanks are intentionally unreachable from this protocol so
    the LLM can't disturb them.

    Args:
        mode: "equipped" (caller's worn backpack) or "world" (placed block).
        dim, x, y, z: Required only for `mode="world"`.

    Returns:
        JSON string with {kind, total_slots, contents_hash, slots:[...]}
        or {error, detail}. Errors include backpack_unequipped (mode=equipped
        but caller is not wearing one) and wrong_target_type (mode=world
        but block isn't a backpack).
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return json.dumps({"error": "no_caller", "detail": "CALLER_PLAYER unset"})
    if mode == "equipped":
        return json.dumps(_do_read("backpack_equipped", caller=caller))
    elif mode == "world":
        if not dim:
            return json.dumps({"error": "bad_args", "detail": "world mode requires dim/x/y/z"})
        return json.dumps(_do_read("backpack_world", caller=caller, dim=dim, x=x, y=y, z=z))
    else:
        return json.dumps({"error": "bad_args", "detail": "mode must be 'equipped' or 'world'"})


def _do_preview(kind: str, new_layout: Any, *, dim: str | None = None,
                x: int | None = None, y: int | None = None, z: int | None = None,
                backpack_mode: str | None = None) -> str:
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return json.dumps({"error": "no_caller", "detail": "CALLER_PLAYER unset"})
    if isinstance(new_layout, str):
        try:
            new_layout = json.loads(new_layout)
        except Exception as e:
            return json.dumps({"error": "bad_layout", "detail": f"layout JSON parse failed: {e}"})
    rd = _do_read(kind, caller=caller, dim=dim, x=x, y=y, z=z)
    if not rd.get("ok"):
        return json.dumps(rd)
    val = _validate_layout(rd["slots"], rd["total_slots"], new_layout)
    if not val.get("ok"):
        return json.dumps(val)
    target_stacks = val["target_stacks"]
    diff = _render_diff(rd["slots"], target_stacks, rd["total_slots"])
    _gc_previews()
    txn_id = uuid.uuid4().hex[:16]
    PREVIEW_STATE[txn_id] = {
        "caller": caller,
        "kind": kind,
        "dim": dim, "x": x, "y": y, "z": z,
        "backpack_mode": backpack_mode,
        "total_slots": rd["total_slots"],
        "contents_hash": rd["contents_hash"],
        "snapshot_contents": rd["slots"],
        "target_stacks": target_stacks,
        "expires_at": time.time() + PREVIEW_TTL_S,
    }
    out: dict[str, Any] = {
        "ok": True,
        "txn_id": txn_id,
        "diff": diff,
        "expires_in_s": PREVIEW_TTL_S,
    }
    if val.get("unmovable"):
        out["unmovable"] = val["unmovable"]
    return json.dumps(out)


@mcp.tool()
def preview_container_reorg(dim: str, x: int, y: int, z: int, new_layout: Any) -> str:
    """
    Preview a chest/barrel/shulker reorganization. Returns a `txn_id`
    bound to the current contents hash; pass that txn_id to
    commit_container_reorg to apply.

    Layout format (use whichever shape is more natural):
      dict: {"<target_slot>": [<source_slot>, ...], ...}
        e.g. {"0": [3, 7], "1": [2]} = target 0 gets sources 3+7 merged,
                                       target 1 gets source 2 unchanged.
      list: [[3, 7], [2], null, ...] = target i gets the i-th element's
                                       source list (null = empty target).

    Constraints:
      - Every non-empty source slot must be placed somewhere (no item loss).
      - Each source slot can be referenced at most once.
      - Sources merged into one target must share an item id.
      - Merged count must not exceed 64 (vanilla stack cap).

    The preview does NOT touch the world; nothing changes until commit.

    Args:
        dim, x, y, z: Container coordinates.
        new_layout: Slot mapping (see above). Accepts JSON string or dict/list.

    Returns:
        JSON {ok, txn_id, diff, expires_in_s} or {error, detail}.
    """
    return _do_preview("container", new_layout, dim=dim, x=x, y=y, z=z)


@mcp.tool()
def preview_inventory_reorg(new_layout: Any) -> str:
    """
    Preview a reorganization of the requesting player's main inventory
    (slots 9–35, dense indices 0–26 — see read_inventory).

    Same layout format and constraints as preview_container_reorg.

    Args:
        new_layout: Slot mapping (see preview_container_reorg).

    Returns:
        JSON {ok, txn_id, diff, expires_in_s} or {error, detail}.
    """
    return _do_preview("inventory", new_layout)


@mcp.tool()
def preview_backpack_reorg(mode: str, new_layout: Any,
                           dim: str = "", x: int = 0, y: int = 0, z: int = 0) -> str:
    """
    Preview a Travelers Backpack reorganization. Same layout format and
    constraints as preview_container_reorg.

    Args:
        mode: "equipped" or "world".
        new_layout: Slot mapping.
        dim, x, y, z: Required only for `mode="world"`.

    Returns:
        JSON {ok, txn_id, diff, expires_in_s} or {error, detail}.
    """
    if mode == "equipped":
        return _do_preview("backpack_equipped", new_layout, backpack_mode="equipped")
    elif mode == "world":
        if not dim:
            return json.dumps({"error": "bad_args", "detail": "world mode requires dim/x/y/z"})
        return _do_preview("backpack_world", new_layout, dim=dim, x=x, y=y, z=z, backpack_mode="world")
    else:
        return json.dumps({"error": "bad_args", "detail": "mode must be 'equipped' or 'world'"})


@mcp.tool()
def commit_container_reorg(txn_id: str) -> str:
    """
    Apply a previewed reorganization. Always preview first — this tool
    needs a fresh txn_id from preview_*_reorg. The txn captures the
    target identity (chest coords / inventory / backpack) so commit
    is a single arg.

    On success, persists a snapshot of the pre-commit contents to the
    bridge's PVC (last 5 per player) and returns its snapshot_id.
    Surface the snapshot_id back to the player so they know how to
    invoke `undo_container_reorg`.

    Common errors:
      unknown_txn — the bridge restarted (rare); re-preview.
      stale_txn — someone else moved items in the container between
                  preview and commit; re-read and re-preview.
      container_in_use — a player has the container open; ask them
                         to close it.
      hoppers_attached — a hopper / dropper / hopper-minecart is
                         feeding/pulling; remove it before retrying.
      conservation_failed — the mod's invariant check failed; this
                            should not happen if the preview validated
                            cleanly (file a bug).
      backpack_unequipped — caller took the backpack off after preview.

    Args:
        txn_id: The txn_id returned from preview_*_reorg.

    Returns:
        JSON {ok, snapshot_id, applied} or {error, detail}.
    """
    _gc_previews()
    state = PREVIEW_STATE.pop(txn_id, None)
    if state is None:
        return json.dumps({"error": "unknown_txn",
                           "detail": "no such txn (or expired); re-preview"})
    caller_env = os.environ.get("CALLER_PLAYER", "").strip()
    if state["caller"].lower() != caller_env.lower():
        return json.dumps({"error": "wrong_caller",
                           "detail": "txn was created by a different player"})
    if state.get("is_multi"):
        return json.dumps(_do_commit_multi(state))
    res = _do_commit(state)
    if not res.get("ok"):
        return json.dumps(res)
    try:
        snap_id = _save_snapshot(state, res.get("snapshot_pre_hash", state["contents_hash"]))
        res["snapshot_id"] = snap_id
    except Exception as e:
        print(f"snapshot save failed: {e}", file=sys.stderr)
        res["snapshot_warning"] = f"snapshot save failed: {e}"
    return json.dumps(res)


# ----- multi-target (cross-container) reorg --------------------------------

def _validate_multi_layout(reads: list[dict[str, Any]],
                           layout: Any) -> dict[str, Any]:
    """
    Cross-target version of _validate_layout. `layout` is a flat list:
      [
        {"to": <to_target>, "slot": <to_slot>, "from": [{"t": <src_t>, "s": <src_s>}, ...]},
        ...
      ]
    Each (target_idx, slot) source must be referenced exactly once across
    the entire layout (item conservation). Merges into one target slot
    require matching item id. Returns
    {ok, target_stacks: [<target_idx -> {slot -> {i, c, n}}>, ...]}.
    """
    if isinstance(layout, str):
        try:
            layout = json.loads(layout)
        except Exception as e:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"layout JSON parse failed: {e}"}
    if not isinstance(layout, list):
        return {"ok": False, "error": "bad_layout",
                "detail": "layout must be a list of placements"}

    by_ts: dict[tuple[int, int], dict[str, Any]] = {}
    unmovable: set[tuple[int, int]] = set()
    for ti, rd in enumerate(reads):
        for s in rd["slots"]:
            key = (ti, int(s["slot"]))
            by_ts[key] = s
            n = s.get("n") or ""
            if len(n) > ITEM_MOVE_NBT_MAX:
                unmovable.add(key)

    used: dict[tuple[int, int], tuple[int, int]] = {}
    target_stacks: list[dict[int, dict[str, Any]]] = [dict() for _ in reads]

    for entry in layout:
        if not isinstance(entry, dict):
            return {"ok": False, "error": "bad_layout",
                    "detail": "each entry must be a dict {to, slot, from}"}
        try:
            to_t = int(entry["to"])
            to_s = int(entry["slot"])
            froms = entry.get("from") or []
        except (KeyError, ValueError, TypeError) as e:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"entry missing/bad fields: {e}"}
        if to_t < 0 or to_t >= len(reads):
            return {"ok": False, "error": "bad_layout",
                    "detail": f"target {to_t} out of range [0, {len(reads)})"}
        if to_s < 0 or to_s >= reads[to_t]["total_slots"]:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"target {to_t} slot {to_s} out of range"}
        if not froms:
            continue

        first_key = (int(froms[0]["t"]), int(froms[0]["s"]))
        first = by_ts.get(first_key)
        if first is None:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"source {first_key} is empty"}
        item_id = first["i"]
        merged = 0
        for f in froms:
            ts_key = (int(f["t"]), int(f["s"]))
            if ts_key in used:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"source {ts_key} used by target {used[ts_key]} and target ({to_t},{to_s})"}
            stk = by_ts.get(ts_key)
            if stk is None:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"source {ts_key} is empty"}
            if stk["i"] != item_id:
                return {"ok": False, "error": "bad_layout",
                        "detail": f"can't merge {item_id} with {stk['i']} from {ts_key}"}
            # Unmovable-item constraint: huge-NBT items must stay in place.
            if ts_key in unmovable and ts_key != (to_t, to_s):
                return {"ok": False, "error": "unmovable_item",
                        "detail": (f"item at target {ts_key[0]} slot {ts_key[1]} has "
                                   f"NBT > {ITEM_MOVE_NBT_MAX} bytes and must stay in place; "
                                   "place its source identically to its current location")}
            used[ts_key] = (to_t, to_s)
            merged += int(stk["c"])
        if merged > 64:
            return {"ok": False, "error": "bad_layout",
                    "detail": f"target ({to_t},{to_s}): merged count {merged} > 64"}
        target_stacks[to_t][to_s] = {
            "i": item_id, "c": merged, "n": first.get("n"),
        }

    all_sources = set(by_ts.keys())
    unplaced = all_sources - set(used.keys())
    if unplaced:
        return {"ok": False, "error": "bad_layout",
                "detail": f"unplaced sources (would be lost): {sorted(unplaced)}"}

    return {"ok": True, "target_stacks": target_stacks, "unmovable": sorted(unmovable)}


def _render_multi_diff(reads: list[dict[str, Any]],
                       target_stacks: list[dict[int, dict[str, Any]]]) -> str:
    out_lines: list[str] = []
    for ti, rd in enumerate(reads):
        before = {s["slot"]: s for s in rd["slots"]}
        after = target_stacks[ti]
        target_changes: list[str] = []
        for i in range(rd["total_slots"]):
            b = before.get(i)
            a = after.get(i)
            if b is None and a is None:
                continue
            b_str = "·" if b is None else f"{_short_id(b['i'])}×{b['c']}"
            a_str = "·" if a is None else f"{_short_id(a['i'])}×{a['c']}"
            if b_str == a_str:
                continue
            target_changes.append(f"  slot {i:>2}: {b_str:<24} → {a_str}")
        if target_changes:
            kind = rd.get("kind", "?")
            out_lines.append(f"target {ti} ({kind}):")
            out_lines.extend(target_changes)
    return "\n".join(out_lines) if out_lines else "(no slot changes)"


def _save_compound_snapshot(caller: str, component_ids: list[str]) -> str:
    snap_id = f"{int(time.time() * 1000)}-c{uuid.uuid4().hex[:6]}"
    pdir = SNAPSHOT_DIR / re.sub(r"[^A-Za-z0-9_\-]", "_", caller)
    pdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_id": snap_id,
        "compound": True,
        "component_ids": component_ids,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
    }
    tmp = pdir / f".{snap_id}.tmp"
    final = pdir / f"{snap_id}.json"
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, final)
    # Compound snapshots count toward the same ring-buffer budget.
    files = sorted([p for p in pdir.iterdir() if p.suffix == ".json"], key=lambda p: p.name)
    while len(files) > SNAPSHOTS_PER_PLAYER:
        try: files[0].unlink()
        except Exception: pass
        files = files[1:]
    return snap_id


def _do_commit_multi(state: dict[str, Any]) -> dict[str, Any]:
    """
    Sequentially commit each target via _do_commit (mode=multi so the mod
    skips per-target conservation). On failure, rollback already-committed
    targets via the restore path.
    """
    caller = state["caller"]
    targets = state["targets"]
    reads = state["reads"]
    target_stacks_list = state["target_stacks"]
    committed: list[tuple[int, dict[str, Any]]] = []
    snapshot_ids: list[str] = []

    for ti, tgt in enumerate(targets):
        kind = tgt["kind"]
        sub_state = {
            "caller": caller,
            "kind": kind,
            "dim": tgt.get("dim"), "x": tgt.get("x"),
            "y": tgt.get("y"), "z": tgt.get("z"),
            "backpack_mode": ("equipped" if kind == "backpack_equipped"
                              else "world" if kind == "backpack_world" else None),
            "total_slots": reads[ti]["total_slots"],
            "contents_hash": reads[ti]["contents_hash"],
            "snapshot_contents": reads[ti]["slots"],
            "target_stacks": target_stacks_list[ti],
            "mode": "multi",
        }
        # Skip targets with no actual changes — saves an RCON round-trip
        # and avoids triggering the viewer guard for unchanged targets.
        if not _target_has_changes(reads[ti], target_stacks_list[ti]):
            committed.append((ti, sub_state))
            continue

        res = _do_commit(sub_state)
        if not res.get("ok"):
            # Rollback previously-committed targets in reverse order.
            rollback_failures: list[dict[str, Any]] = []
            for prev_ti, prev_state in reversed(committed):
                if prev_state.get("_skipped"):
                    continue
                # Build a restore from the original snapshot_contents.
                prev_state_restore = dict(prev_state)
                prev_state_restore["mode"] = "restore"
                prev_state_restore["target_stacks"] = {
                    int(s["slot"]): {"i": s["i"], "c": int(s["c"]), "n": s.get("n")}
                    for s in prev_state["snapshot_contents"]
                }
                rb = _do_commit(prev_state_restore)
                if not rb.get("ok"):
                    rollback_failures.append({"target": prev_ti, "error": rb})
                    print(f"ROLLBACK FAILED for target {prev_ti}: {rb}", file=sys.stderr)
            err = dict(res)
            err["target_failed"] = ti
            err["rolled_back"] = [t for t, _ in committed]
            if rollback_failures:
                err["rollback_failures"] = rollback_failures
            return err

        try:
            snap_id = _save_snapshot(
                sub_state, res.get("snapshot_pre_hash", reads[ti]["contents_hash"])
            )
            snapshot_ids.append(snap_id)
        except Exception as e:
            print(f"snapshot save failed for target {ti}: {e}", file=sys.stderr)
        committed.append((ti, sub_state))

    if not snapshot_ids:
        return {"ok": True, "applied": 0, "detail": "no targets had changes"}
    if len(snapshot_ids) == 1:
        return {"ok": True, "snapshot_id": snapshot_ids[0],
                "applied": len(committed), "targets": len(targets)}
    compound_id = _save_compound_snapshot(caller, snapshot_ids)
    return {"ok": True, "snapshot_id": compound_id,
            "component_snapshots": snapshot_ids,
            "applied": len(committed), "targets": len(targets)}


def _target_has_changes(read: dict[str, Any],
                        target_stacks: dict[int, dict[str, Any]]) -> bool:
    before = {s["slot"]: s for s in read["slots"]}
    for i in range(read["total_slots"]):
        b = before.get(i)
        a = target_stacks.get(i)
        if (b is None) != (a is None):
            return True
        if b is None:
            continue
        if b["i"] != a["i"] or int(b["c"]) != int(a["c"]):
            return True
    return False


@mcp.tool()
def preview_reorg(targets: Any, layout: Any) -> str:
    """
    Preview a reorganization that may move items between multiple
    containers / inventories / backpacks. Use this when the player
    asks for a cross-container sort (e.g. "consolidate my inventory,
    backpack, and the chest in front of me").

    Args:
        targets: List of target descriptors. Each is one of:
            {"kind": "container",         "dim": "<id>", "x":int, "y":int, "z":int}
            {"kind": "inventory"}
            {"kind": "backpack_equipped"}
            {"kind": "backpack_world",    "dim": "<id>", "x":int, "y":int, "z":int}
            Order matters: target_idx in the layout refers to position in this list.
        layout: List of placements. Each entry:
            {"to": <to_target_idx>, "slot": <to_slot>,
             "from": [{"t": <src_target_idx>, "s": <src_slot>}, ...]}
            Each (target_idx, slot) source must appear exactly once across
            all entries. Merging multiple sources into one target slot
            requires same item id and total count ≤ 64.

    Returns:
        JSON {ok, txn_id, diff, expires_in_s} on success, or {error, detail}.
        For commit, pass txn_id to commit_container_reorg.

    Special cases:
      - Items with NBT base64 > {ITEM_MOVE_NBT_MAX} bytes (e.g. very large
        modded books) are flagged "unmovable": layout must keep them in
        their current (target, slot). This avoids straining the RCON
        protocol with multi-MB chunked writes.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return json.dumps({"error": "no_caller", "detail": "CALLER_PLAYER unset"})

    if isinstance(targets, str):
        try:
            targets = json.loads(targets)
        except Exception as e:
            return json.dumps({"error": "bad_args",
                               "detail": f"targets JSON parse failed: {e}"})
    if not isinstance(targets, list) or not targets:
        return json.dumps({"error": "bad_args",
                           "detail": "targets must be a non-empty list"})

    reads: list[dict[str, Any]] = []
    for ti, tgt in enumerate(targets):
        if not isinstance(tgt, dict) or "kind" not in tgt:
            return json.dumps({"error": "bad_args",
                               "detail": f"target {ti}: missing 'kind'"})
        kind = tgt["kind"]
        if kind == "container":
            rd = _do_read("container", caller=caller, dim=tgt.get("dim"),
                          x=tgt.get("x"), y=tgt.get("y"), z=tgt.get("z"))
        elif kind == "inventory":
            rd = _do_read("inventory", caller=caller)
        elif kind == "backpack_equipped":
            rd = _do_read("backpack_equipped", caller=caller)
        elif kind == "backpack_world":
            rd = _do_read("backpack_world", caller=caller, dim=tgt.get("dim"),
                          x=tgt.get("x"), y=tgt.get("y"), z=tgt.get("z"))
        else:
            return json.dumps({"error": "bad_args",
                               "detail": f"target {ti}: unknown kind '{kind}'"})
        if not rd.get("ok"):
            err = dict(rd)
            err["target"] = ti
            return json.dumps(err)
        reads.append(rd)

    val = _validate_multi_layout(reads, layout)
    if not val.get("ok"):
        return json.dumps(val)

    diff = _render_multi_diff(reads, val["target_stacks"])
    _gc_previews()
    txn_id = uuid.uuid4().hex[:16]
    PREVIEW_STATE[txn_id] = {
        "caller": caller,
        "is_multi": True,
        "targets": targets,
        "reads": reads,
        "target_stacks": val["target_stacks"],
        "expires_at": time.time() + PREVIEW_TTL_S,
    }
    out = {"ok": True, "txn_id": txn_id, "diff": diff, "expires_in_s": PREVIEW_TTL_S}
    if val.get("unmovable"):
        out["unmovable"] = val["unmovable"]
    return json.dumps(out)


@mcp.tool()
def undo_container_reorg(snapshot_id: str) -> str:
    """
    Restore a previous container/inventory/backpack layout from a snapshot.

    Use when a player wants to undo a reorg you just performed. The
    snapshot_id was returned by `commit_container_reorg`. Each player
    keeps the 5 most recent snapshots; older ones are dropped.

    The mod re-validates that the player is still in range and (for
    backpack_equipped) is still wearing the backpack. The hopper-attached
    check is intentionally SKIPPED in restore mode so a hopper added
    after the original commit doesn't permanently brick undo. The TOCTOU
    contents-hash check is skipped (we're explicitly overwriting); item
    conservation is also skipped because the snapshot is authoritative.

    Args:
        snapshot_id: The id returned by `commit_container_reorg`.

    Returns:
        JSON {ok, applied} or {error, detail}.
    """
    caller = os.environ.get("CALLER_PLAYER", "").strip()
    if not caller:
        return json.dumps({"error": "no_caller", "detail": "CALLER_PLAYER unset"})
    pdir = SNAPSHOT_DIR / re.sub(r"[^A-Za-z0-9_\-]", "_", caller)
    snap_path = pdir / f"{snapshot_id}.json"
    if not snap_path.exists():
        return json.dumps({"error": "unknown_snapshot",
                           "detail": f"no snapshot {snapshot_id} for {caller}"})
    try:
        snap = json.loads(snap_path.read_text())
    except Exception as e:
        return json.dumps({"error": "bad_snapshot", "detail": str(e)})
    if snap.get("caller", "").lower() != caller.lower():
        return json.dumps({"error": "wrong_caller", "detail": "snapshot belongs to another player"})

    # Compound (multi-target) snapshot — restore each component in reverse
    # order so the most-recently-committed target is reverted first
    # (mirrors the logical undo order).
    if snap.get("compound"):
        components = snap.get("component_ids", [])
        results: list[dict[str, Any]] = []
        all_ok = True
        for sub_id in reversed(components):
            sub_res = json.loads(undo_container_reorg(sub_id))
            results.append({"component": sub_id, "result": sub_res})
            if not sub_res.get("ok"):
                all_ok = False
        return json.dumps({"ok": all_ok, "components": results})

    # Build target_stacks from snapshot_contents directly.
    target_stacks = {}
    for s in snap["snapshot_contents"]:
        target_stacks[int(s["slot"])] = {
            "i": s["i"], "c": int(s["c"]), "n": s.get("n"),
        }

    state = {
        "caller": caller,
        "kind": snap["kind"],
        "dim": snap.get("dim"), "x": snap.get("x"), "y": snap.get("y"), "z": snap.get("z"),
        "backpack_mode": snap.get("backpack_mode"),
        "total_slots": int(snap["total_slots"]),
        "contents_hash": snap["pre_commit_contents_hash"],
        "snapshot_contents": snap["snapshot_contents"],
        "target_stacks": target_stacks,
        "mode": "restore",
    }
    # Restore mode bypasses the hash precheck — but the mod still expects
    # *some* expected_hash arg; pass the snapshot's pre-hash even though
    # the mod will skip validating it in restore mode.
    res = _do_commit(state)
    return json.dumps(res)


if __name__ == "__main__":
    mcp.run()
