#!/usr/bin/env bash
# Put the submodule worktrees back on the commits this repo records.
#
# WHY THIS EXISTS
# `git pull` moves the gitlinks in the index and leaves the worktrees where they
# were. Nothing warns you. The pins here move on a daily timer in a *different*
# clone (scripts/systemd/selfhosted-submodules-sync.service runs out of
# ~/Code/selfhosted), so every other clone pulls a pin it never checks out and
# from then on reports ` M web/whatnowgg` forever.
#
# That dirty gitlink is not a change — it is this clone being behind. Commit it
# with a broad `git add -A` and the pin travels BACKWARDS: the repo starts
# claiming the cluster runs an older commit than it does, which is the exact
# failure sync-submodules.sh was written to prevent, arriving through the back
# door. It happened to web/whatnowgg between 2026-08-13 and 2026-08-14.
#
# So: after every merge and every branch checkout, put the worktrees back.
# .githooks/post-merge and .githooks/post-checkout call this.
#
# The unlock/lock dance is scripts/submodules-lock.sh's doing — the worktrees are
# chmod a-w so nothing accidentally works inside one, and a checkout needs write
# access. Locking again is in a trap so a failed run cannot leave them writable.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCKER="${ROOT}/scripts/submodules-lock.sh"
cd "$ROOT"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
say() { [[ $QUIET -eq 1 ]] || echo "$@"; }

# `git submodule status` prefixes a line with + when the checked-out commit is
# not the recorded one, and - when the submodule was never initialised. Both
# want the same fix; anything else is already correct and gets out of the way
# without a chmod -R over eight worktrees (talaria is not small).
drift="$(git submodule status --cached 2>/dev/null | grep -c '^[+-]' || true)"
if [[ "${drift:-0}" -eq 0 ]]; then
  say "submodules: already on their recorded commits"
  exit 0
fi

relock() { [[ -x "$LOCKER" ]] && "$LOCKER" lock >/dev/null 2>&1 || true; }
if [[ -x "$LOCKER" ]]; then trap relock EXIT INT TERM; "$LOCKER" unlock >/dev/null 2>&1 || true; fi

say "submodules: ${drift} worktree(s) off their pin — checking out what this repo records"
if git submodule update --init --recursive 2>&1 | sed 's/^/    /'; then
  say "submodules: back in sync"
else
  # Never fail the merge or checkout that called us: the pull did happen, and a
  # network hiccup fetching a submodule is not a reason to make it look broken.
  echo "submodules: refresh failed — run scripts/submodules-refresh.sh by hand" >&2
fi

exit 0
