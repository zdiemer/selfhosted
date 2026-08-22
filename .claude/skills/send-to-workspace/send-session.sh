#!/usr/bin/env bash
# Migrate a Claude Code session from this machine into the claude-workspace
# pod, where it can be resumed from Signal/WhatsApp (messaging gateway),
# Happy, or the /term tmux.
#
#   send-session.sh [session-uuid] [--target <pod-cwd>] [--force]
#
# A session is just ~/.claude/projects/<munged-cwd>/<uuid>.jsonl (plus an
# optional <uuid>/subagents/ tree), where <munged-cwd> is the absolute cwd
# with '/' and '.' replaced by '-'. Every record embeds that absolute cwd,
# and resume looks the session up by munged directory name — so migration is
# a copy that rewrites the local cwd to the pod's checkout of the same repo,
# both in the directory name and in each record's top-level `cwd` field.
# Nothing else is rewritten: other absolute paths in message content are
# historical record, and a textual find-and-replace could corrupt payloads
# that merely contain the same byte sequence. The rewrite is per-line jq,
# guarded on has("cwd") because queue-operation records carry no cwd.
#
# The pod's NetworkPolicy blocks LAN egress, so this pushes from outside via
# `kubectl exec` streaming (exec + cat, the house style — see the note in
# scripts/egress-audit.sh about `kubectl cp`). The write lands as .partial
# and is mv'd into place: the gateway's bare `!resume` picks the mtime-newest
# jsonl in the project dir, and must never see a half-written file. That
# fresh mtime is also what makes bare `!resume` find the migrated session.
#
# This COPIES, never moves — the local transcript stays behind as fallback.
# It deliberately does not touch the gateway's state.json (cached in-process;
# external writes get clobbered) or the pod's ~/.claude.json (claude may be
# live there). Handoff is completed by the printed resume commands.

set -euo pipefail

POD_HOME=/home/node
POD_CODE_ROOT="$POD_HOME/code"
NAMESPACE="${NAMESPACE:-claude}"
TARGET="${TARGET:-deploy/claude-workspace}"

die() { echo "error: $*" >&2; exit 1; }
munge() { printf '%s' "$1" | tr '/.' '--'; }
kx()  { kubectl -n "$NAMESPACE" exec "$TARGET" -c term -- "$@"; }
kxi() { kubectl -n "$NAMESPACE" exec -i "$TARGET" -c term -- "$@"; }

REWRITE='if has("cwd") and (.cwd | type == "string")
            and ((.cwd == $old) or (.cwd | startswith($old + "/")))
         then .cwd = $new + (.cwd[($old | length):]) else . end'

# --- arguments --------------------------------------------------------------
uuid="" target_cwd="" force=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) target_cwd="$2"; shift 2 ;;
    --force)  force=1; shift ;;
    -*)       die "unknown flag: $1" ;;
    *)        [[ -n "$uuid" ]] && die "unexpected argument: $1"; uuid="$1"; shift ;;
  esac
done

command -v jq      >/dev/null || die "jq required"
command -v kubectl >/dev/null || die "kubectl required"
kubectl -n "$NAMESPACE" get "$TARGET" >/dev/null 2>&1 \
  || die "kubectl can't see $TARGET in namespace '$NAMESPACE' — is KUBECONFIG pointed at the homelab cluster?"

# --- locate the session locally ---------------------------------------------
if [[ -z "$uuid" && -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  uuid="$CLAUDE_CODE_SESSION_ID"
fi

jsonl=""
if [[ -n "$uuid" ]]; then
  [[ "$uuid" =~ ^[0-9a-fA-F-]{36}$ ]] || die "not a session uuid: $uuid"
  for f in "$HOME/.claude/projects"/*/"$uuid.jsonl"; do
    [[ -e "$f" ]] && jsonl="$f" && break
  done
  [[ -n "$jsonl" ]] || die "no transcript for session $uuid under ~/.claude/projects"
else
  # No uuid available: newest jsonl for the current directory, the same
  # rule the gateway's latestSessionId() applies.
  project_dir="$HOME/.claude/projects/$(munge "$PWD")"
  [[ -d "$project_dir" ]] || die "no sessions recorded for $PWD"
  jsonl="$(ls -1t "$project_dir"/*.jsonl 2>/dev/null | head -1 || true)"
  [[ -n "$jsonl" ]] || die "no transcripts in $project_dir"
  uuid="$(basename "$jsonl" .jsonl)"
fi

src_cwd="$(jq -r 'select(has("cwd")) | .cwd' "$jsonl" | tail -1)"
[[ -n "$src_cwd" && "$src_cwd" != null ]] || die "transcript has no cwd records: $jsonl"

# --- map to a pod cwd -------------------------------------------------------
if [[ -n "$target_cwd" ]]; then
  kx test -d "$target_cwd" || die "--target $target_cwd does not exist in the pod"
  dst_cwd="$target_cwd"
else
  rel=""
  for root in "$HOME/Code" "$HOME/code"; do
    [[ "$src_cwd" == "$root"/* ]] && rel="${src_cwd#"$root"/}" && break
  done
  [[ -n "$rel" ]] || die "session cwd $src_cwd is not under ~/Code or ~/code — pass --target <pod-cwd>"
  dst_cwd="$POD_CODE_ROOT/$rel"
  if ! kx test -d "$dst_cwd" 2>/dev/null; then
    # Worktrees and scratch checkouts may not exist in the pod; land the
    # session at the repo root instead. The resumed session re-orients.
    repo_root="$POD_CODE_ROOT/${rel%%/*}"
    kx test -d "$repo_root" 2>/dev/null \
      || die "neither $dst_cwd nor $repo_root exists in the pod — pass --target <pod-cwd>"
    echo "==> $dst_cwd not in the pod; landing at $repo_root"
    dst_cwd="$repo_root"
  fi
fi
[[ "$dst_cwd" != "$src_cwd" ]] || die "this session already lives at $src_cwd on the workspace"

# --- rewrite into staging ---------------------------------------------------
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

jq -c --arg old "$src_cwd" --arg new "$dst_cwd" "$REWRITE" "$jsonl" > "$stage/$uuid.jsonl"
[[ "$(wc -l < "$jsonl")" -eq "$(wc -l < "$stage/$uuid.jsonl")" ]] \
  || die "rewrite changed the line count — refusing to ship"

src_extra="$(dirname "$jsonl")/$uuid"
if [[ -d "$src_extra" ]]; then
  while IFS= read -r -d '' f; do
    out="$stage/$uuid/${f#"$src_extra/"}"
    mkdir -p "$(dirname "$out")"
    case "$f" in
      *.jsonl)     jq -c --arg old "$src_cwd" --arg new "$dst_cwd" "$REWRITE" "$f" > "$out" ;;
      *.meta.json) jq    --arg old "$src_cwd" --arg new "$dst_cwd" "$REWRITE" "$f" > "$out" ;;
      *)           cp "$f" "$out" ;;
    esac
  done < <(find "$src_extra" -type f -print0)
fi

# --- collision check --------------------------------------------------------
dest_dir="$POD_HOME/.claude/projects/$(munge "$dst_cwd")"
dest="$dest_dir/$uuid.jsonl"
staged_sha="$(sha256sum "$stage/$uuid.jsonl" | cut -d' ' -f1)"

if kx test -e "$dest" 2>/dev/null; then
  pod_sha="$(kx sha256sum "$dest" | cut -d' ' -f1)"
  if [[ "$pod_sha" == "$staged_sha" ]]; then
    echo "==> already on the workspace, unchanged ($dest)"
    exit 0
  fi
  if [[ "$force" -ne 1 ]]; then
    die "$dest exists in the pod with different content — a diverged copy. Re-run with --force to overwrite it."
  fi
fi

# --- ship -------------------------------------------------------------------
gzip -c "$stage/$uuid.jsonl" | kxi sh -c \
  "mkdir -p '$dest_dir' && gzip -d > '$dest.partial' && mv '$dest.partial' '$dest'"

if [[ -d "$stage/$uuid" ]]; then
  tar -C "$stage" -czf - "$uuid" | kxi tar -xzf - -C "$dest_dir"
fi

pod_sha="$(kx sha256sum "$dest" | cut -d' ' -f1)"
[[ "$pod_sha" == "$staged_sha" ]] || die "checksum mismatch after upload ($pod_sha != $staged_sha)"

# --- handoff ----------------------------------------------------------------
lines="$(wc -l < "$stage/$uuid.jsonl")"
cat <<EOF

✓ session ${uuid:0:8} → workspace:$dst_cwd  ($lines lines, checksum verified)

Resume from:
  Signal/WhatsApp:   !cwd $dst_cwd
                     !resume $uuid     (or bare !resume — it's now the newest)
  Happy (in /term):  cd $dst_cwd && happy --resume $uuid
  tmux  (in /term):  cd $dst_cwd && claude --resume $uuid

⚠ Stop driving this session here. The copy is a snapshot — anything after
  this point stays on this machine, and two surfaces writing one session
  interleave the transcript.
EOF
