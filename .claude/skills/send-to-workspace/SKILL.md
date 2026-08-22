---
name: send-to-workspace
description: >-
  Migrate the current Claude Code session (or a named session uuid) from this
  machine into the claude-workspace pod, so it can be resumed from
  Signal/WhatsApp via the messaging gateway, from Happy, or in the /term tmux.
  Triggers on "send this session to the workspace", "send this to my phone /
  Signal / WhatsApp", "hand off this session", "continue this from Happy",
  "move this session to the pod". Requires kubectl access to the homelab
  cluster from this machine.
---

# Send this session to the workspace

A session is `~/.claude/projects/<munged-cwd>/<uuid>.jsonl` (plus an optional
`<uuid>/subagents/` tree), where `<munged-cwd>` is the absolute cwd with `/`
and `.` replaced by `-`. Every record embeds that cwd, and resume looks the
session up by munged directory name — so "migrating" a session means copying
the transcript into the pod's `~/.claude/projects/` with the local cwd
rewritten to the pod's checkout of the same repo. The pod cannot reach this
machine (its NetworkPolicy blocks LAN egress), so the copy is pushed in over
`kubectl exec`. All of that is one script:

```
.claude/skills/send-to-workspace/send-session.sh [session-uuid] [--target <pod-cwd>] [--force]
```

With no arguments it sends the **current** session (`$CLAUDE_CODE_SESSION_ID`
is in the Bash environment). Forward a uuid or `--target` if the user named
one. The cwd maps `~/Code/<repo>/...` or `~/code/<repo>/...` to
`/home/node/code/<repo>/...`; if that exact directory (say, a git worktree)
doesn't exist in the pod, it falls back to the repo root there.

**Run the script as the last action of the turn, echo its handoff block to
the user verbatim, and stop.** The copy is a snapshot taken when the script
runs — every further line of conversation widens the gap the workspace copy
doesn't have.

## Never do these

- **Never edit the gateway's `state.json`** to point a chat at the migrated
  session. The gateway caches it in-process and clobbers external writes on
  its next save. The printed `!cwd` + `!resume` commands are the supported
  path (`dev/claude-workspace/gateway/src/router.ts`).
- **Never edit the pod's `~/.claude.json`** (e.g. `lastSessionId` so
  `claude -c` works). Claude may be live in the pod, and a concurrent write
  can corrupt it. Explicit `--resume <uuid>` needs none of it.
- **Never move or delete the local transcript.** The script copies; the
  local file is the fallback if the handoff goes wrong.

## What deliberately doesn't migrate

`~/.claude/session-env/<uuid>`, `~/.claude/tasks/<uuid>`, and todos reference
processes and paths on this machine and are useless in the pod; resume works
without them. Only the transcript and its `subagents/` tree travel.

## Gotchas

- **mtime is load-bearing.** Bare `!resume` in a gateway chat picks the
  newest jsonl in the project dir by mtime (`gateway/src/claude.ts`,
  `latestSessionId`). The script ships via `.partial` + `mv` precisely so the
  freshly-landed file is newest and never seen half-written.
- **One surface per session.** Driving the same session from here and from
  the workspace at once interleaves jsonl writes
  (`dev/claude-workspace/README.md`, messaging gotchas). After sending, this
  machine's copy is dead to you unless the handoff failed.
- **Worktree fallback shifts context.** A session from a worktree that
  doesn't exist in the pod lands at the repo root — different branch state.
  Claude re-orients, but the user should know which checkout they're now in;
  the script prints it.
- **The pod copy is not byte-identical to the local file.** The rewrite is
  per-line jq, which normalizes unicode escapes. The script verifies the pod
  copy against the *staged rewrite's* checksum; comparing against the
  original local file will always "fail".

## Related

- `dev/claude-workspace/README.md` — resume surfaces, restarts, the
  one-surface-at-a-time rule
- `dev/claude-workspace/gateway/src/claude.ts` — munge rule, `latestSessionId`
- `dev/claude-workspace/gateway/src/router.ts` — `!cwd` / `!resume` semantics
