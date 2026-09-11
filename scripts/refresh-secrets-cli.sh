#!/usr/bin/env bash
# Keep the installed `secrets` on PATH in step with scripts/secrets.sh.
#
# WHY. The bundle (scripts/build-secrets-cli.sh) is a COPY cut at build time, so
# every edit to the sources leaves the copy on PATH behind until someone
# remembers to rebuild. secrets.sh's warn_if_bundle_stale already notices — but
# it prints to stderr on a tool whose stdout you are usually reading, and a
# warning nobody acts on is not a mechanism. On 2026-08-13 the bundle was built
# four minutes before 43fac8a landed, and it then sat five commits and eighteen
# days behind, missing f70c141's .dev/worktree/ prune — so every local `secrets
# status` was scanning 93 "charts" and calling a deprecated one UNRESOLVABLE
# while the systemd timer, which runs scripts/secrets.sh directly, was fine.
#
# Called from the git hooks that can move these sources: post-merge (pull),
# post-checkout (branch switch), post-rewrite (rebase) and post-commit (the
# local edit that started it). It is a no-op — one sha256 over three files —
# whenever the copy already matches, which is nearly always.
#
#   --quiet   say nothing unless a rebuild happened or failed
#
# Rebuilds only a copy that ALREADY EXISTS. A machine that never installed the
# CLI does not get one silently created by a git pull.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
note() { [[ $QUIET -eq 1 ]] || echo "refresh-secrets-cli: $*"; }

# The same three files in the same order build-secrets-cli.sh hashes. Keep in
# step with it and with secrets.sh's warn_if_bundle_stale.
SRC=("${ROOT}/scripts/secrets.sh" "${ROOT}/scripts/op-session.sh" "${ROOT}/scripts/lib/op-signin-pty.py")
for f in "${SRC[@]}"; do
  [[ -f "$f" ]] || { note "missing $f — nothing to do"; exit 0; }
done
WANT="$(cat "${SRC[@]}" | sha256sum | cut -d' ' -f1)"

# Wherever a copy is installed, not just the default: a PATH copy and
# dist/secrets can be in different states, and both are used.
targets=()
installed="$(command -v secrets 2>/dev/null || true)"
[[ -n "$installed" ]] && targets+=("$installed")
[[ -f "${ROOT}/dist/secrets" && "${ROOT}/dist/secrets" != "$installed" ]] && targets+=("${ROOT}/dist/secrets")
[[ ${#targets[@]} -gt 0 ]] || { note "no installed copy — skipping"; exit 0; }

stale=0
for t in "${targets[@]}"; do
  # A `secrets` on PATH that is not our bundle has no BUNDLE_SRC_HASH; leave it
  # alone rather than overwriting someone else's tool of the same name.
  have="$(grep -m1 '^BUNDLE_SRC_HASH=' "$t" 2>/dev/null | cut -d= -f2)"
  [[ -n "$have" ]] || { note "$t is not a secrets bundle — leaving it alone"; continue; }
  [[ "$have" == "$WANT" ]] || { stale=1; note "$t is behind ($have)"; }
done
[[ $stale -eq 1 ]] || exit 0

# Build to dist/ then install, which is exactly what a human would run.
# INSTALL_DIR follows the copy that is actually on PATH — the builder defaults to
# ~/.local/bin, and defaulting here would install a second copy somewhere the
# shell never looks while the stale one stayed first in $PATH.
INSTALL_DIR_ARG="$HOME/.local/bin"
[[ -n "$installed" ]] && INSTALL_DIR_ARG="$(dirname "$installed")"
# Failure is loud even under --quiet: a silent one puts you back where you started.
if OUT="${ROOT}/dist/secrets" INSTALL_DIR="$INSTALL_DIR_ARG" \
     "${ROOT}/scripts/build-secrets-cli.sh" --install >/dev/null; then
  echo "refresh-secrets-cli: rebuilt \`secrets\` from ${WANT:0:12}"
else
  echo "refresh-secrets-cli: FAILED to rebuild — run ${ROOT}/scripts/build-secrets-cli.sh --install" >&2
  exit 1
fi
