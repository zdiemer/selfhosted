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
# So: after every merge, every branch checkout and every rebase, put the
# worktrees back. .githooks/post-merge, post-checkout and post-rewrite call this.
#
# WHY --force, AND WHY HEAD-vs-PIN IS NOT THE WHOLE CHECK
# A rebase runs post-checkout at its START — checking out the upstream, whose
# pins may be older — and this script then races the rebase itself. On
# 2026-08-19 that left the worst hybrid: submodule HEADs back on the recorded
# pins, worktree FILES still from the older ones. `git submodule update` is a
# no-op when HEAD already matches the pin, so that state was unrepairable by
# rerunning this script, and it read as ` m <path>` in git status forever.
# Hence: stale content counts as drift, and the update runs --force so the
# checkout happens even when HEAD is already right. Nothing of value is ever
# discarded — the worktrees are locked read-only precisely so no real work can
# land in them — but whatever IS discarded goes to a patch under .git/ first.
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

# One refresh at a time. Hooks can stack (a pull --rebase fires post-checkout
# and post-rewrite within seconds) and two runs interleaving their unlock /
# checkout / lock phases is how half-checked-out states are made. The last
# runner always sees the final index, so waiting is correct, not just safe.
exec 9>>"${ROOT}/.git/submodules-refresh.lock"
if ! flock -w 120 9; then
  echo "submodules: another refresh held the lock for 120s — run scripts/submodules-refresh.sh by hand" >&2
  exit 0
fi

submodule_paths() {
  git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'
}

# Two kinds of drift, and both matter:
#   pin drift    `git submodule status --cached` prefixes + (HEAD is not the
#                recorded commit) or - (never initialised).
#   stale files  HEAD IS the recorded commit but the worktree content is not —
#                the 2026-08-19 state. Invisible to the line above.
drift="$(git submodule status --cached 2>/dev/null | grep -c '^[+-]' || true)"
stale=""
while IFS= read -r p; do
  [[ -e "$p/.git" ]] || continue
  git -C "$p" diff --quiet HEAD -- 2>/dev/null || stale="${stale} ${p}"
done < <(submodule_paths)

if [[ "${drift:-0}" -eq 0 && -z "$stale" ]]; then
  say "submodules: already on their recorded commits"
  exit 0
fi

# Keep whatever --force is about to throw away. The locked worktrees make real
# work in here near-impossible, but a patch under .git/ costs nothing and turns
# "it discarded something" from a forensic exercise into a file.
for p in $stale; do
  keep="${ROOT}/.git/submodule-discards"
  mkdir -p "$keep"
  patch="${keep}/$(echo "$p" | tr / -)-$(date +%Y%m%dT%H%M%S).patch"
  git -C "$p" diff HEAD > "$patch" 2>/dev/null || true
  say "submodules: ${p} worktree differs from its HEAD — content saved to ${patch#${ROOT}/}"
done

relock() { [[ -x "$LOCKER" ]] && "$LOCKER" lock >/dev/null 2>&1 || true; }
if [[ -x "$LOCKER" ]]; then trap relock EXIT INT TERM; "$LOCKER" unlock >/dev/null 2>&1 || true; fi

say "submodules: putting worktrees on what this repo records (pin drift: ${drift:-0}, stale content:$( [[ -n "$stale" ]] && echo "$stale" || echo " none"))"
if git submodule update --init --recursive --checkout --force 2>&1 | sed 's/^/    /'; then
  say "submodules: back in sync"
else
  # Never fail the merge or checkout that called us: the pull did happen, and a
  # network hiccup fetching a submodule is not a reason to make it look broken.
  echo "submodules: refresh failed — run scripts/submodules-refresh.sh by hand" >&2
fi

# Trust nothing above: assert the end state. A checkout that half-fails (it
# happened — locked dirs make unlink fail file by file) must not exit looking
# green. This is a report, not a rollback: the loud line is the point.
bad=""
while IFS= read -r p; do
  [[ -e "$p/.git" ]] || continue
  rec="$(git rev-parse "HEAD:${p}" 2>/dev/null)" || continue
  cur="$(git -C "$p" rev-parse HEAD 2>/dev/null || echo '?')"
  if [[ "$rec" != "$cur" ]] || ! git -C "$p" diff --quiet HEAD -- 2>/dev/null; then
    bad="${bad} ${p}"
  fi
done < <(submodule_paths)
if [[ -n "$bad" ]]; then
  echo "submodules: STILL WRONG after refresh:${bad} — investigate before committing anything" >&2
fi

exit 0
