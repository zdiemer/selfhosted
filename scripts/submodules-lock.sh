#!/usr/bin/env bash
# Make the submodule checkouts read-only, so work never lands in one by accident.
#
# WHY
# A submodule here is a revision record, not a place to work: the copy that
# deploys lives in that repo's own clone under ~/Code. An edit made inside a
# submodule worktree looks like it worked — the file changes, helm would even
# render it — but it can never ship, and the superproject reports it as a dirty
# gitlink rather than as the change it is. The failure is silent and it wastes
# the whole edit.
#
# TWO LAYERS, because they stop different things:
#
#   chmod -R a-w     stops writes. Not just Edit/Write from an agent: a stray
#                    `bash -c 'echo … >> file'`, an editor, a build step. This
#                    is the only layer that is actually enforcement.
#   no-push remote   stops a commit made inside a submodule from reaching
#                    GitHub, where it would race the real clone's history.
#
# A third layer lives in .claude/settings.json (deny rules), which is advice to
# agents rather than enforcement — useful because it explains WHERE to work
# instead of just refusing.
#
# Fetching still works while locked: a submodule's real git directory lives in
# the superproject's .git/modules/, which is untouched here. Only checkout needs
# write access, which is why scripts/sync-submodules.sh unlocks around its own
# `git checkout` and locks again afterwards.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "==> $*"; }

[[ -f .gitmodules ]] || die "no .gitmodules at ${ROOT}"

submodule_paths() {
  git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'
}

# An unpopulated submodule (never `git submodule update --init`) is an empty
# directory. Locking it would make the later init fail for no reason.
populated() { [[ -e "$1/.git" ]]; }

lock_one() {
  local p="$1"
  chmod -R a-w "$p"
  # The gitlink file itself must stay readable; chmod -R a-w leaves it so.
  git -C "$p" remote set-url --push origin no-push 2>/dev/null || true
}

unlock_one() {
  local p="$1"
  chmod -R u+w "$p"
}

# Restore the real push URL from .gitmodules, for the rare case of deliberately
# working in a submodule. Deliberate is the point: it takes a command.
unpin_push() {
  local p="$1" name url
  name="$(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' \
          | awk -v p="$p" '$2==p {print $1}' | sed 's/^submodule\.//; s/\.path$//')"
  url="$(git config -f .gitmodules --get "submodule.${name}.url" 2>/dev/null || true)"
  [[ -n "$url" ]] && git -C "$p" remote set-url --push origin "$url"
}

cmd_lock() {
  local p n=0
  while IFS= read -r p; do
    populated "$p" || { echo "    skip (not initialised): $p"; continue; }
    lock_one "$p"; echo "    locked   $p"; n=$((n+1))
  done < <(submodule_paths)
  note "$n submodule(s) read-only, push disabled"
}

cmd_unlock() {
  local p n=0
  while IFS= read -r p; do
    populated "$p" || continue
    unlock_one "$p"; unpin_push "$p"; echo "    unlocked $p"; n=$((n+1))
  done < <(submodule_paths)
  note "$n submodule(s) writable again — re-lock with: scripts/submodules-lock.sh lock"
}

cmd_status() {
  local p w push
  printf '%-32s %-10s %s\n' "SUBMODULE" "WRITABLE" "PUSH URL"
  while IFS= read -r p; do
    if ! populated "$p"; then printf '%-32s %-10s %s\n' "$p" "-" "(not initialised)"; continue; fi
    [[ -w "$p" ]] && w="yes" || w="no"
    push="$(git -C "$p" remote get-url --push origin 2>/dev/null || echo '?')"
    printf '%-32s %-10s %s\n' "$p" "$w" "$push"
  done < <(submodule_paths)
}

usage() {
  cat <<'EOF'
Usage: scripts/submodules-lock.sh [lock|unlock|status]

  lock     make every submodule worktree read-only and disable pushing (default)
  unlock   make them writable again and restore their push URLs
  status   show which are locked

Work in the app's own clone under ~/Code, not in the submodule. The submodule
pin records what shipped; scripts/sync-submodules.sh moves it.
EOF
}

case "${1:-lock}" in
  lock)   cmd_lock ;;
  unlock) cmd_unlock ;;
  status) cmd_status ;;
  -h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
