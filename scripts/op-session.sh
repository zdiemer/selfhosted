#!/usr/bin/env bash
# Hand out a valid 1Password session, without a human in the loop.
#
# WHY THIS EXISTS
# Every write path in scripts/secrets.sh needed an interactive `op signin`, so
# nothing could be put on a timer. That is why the weekly NAS backup ran
# --files-only and silently shipped without its vault dump: an unattended run
# can never hold a session, and a backup that quietly drops half its content is
# the one failure mode a backup must not have.
#
# RESOLUTION ORDER — first hit wins:
#   1. $OP_SERVICE_ACCOUNT_TOKEN   op uses it directly; there is no session to
#                                  manage. Nothing here has to change if this
#                                  account ever moves to a Teams plan.
#   2. the cached token            validated by actually calling op, not by
#                                  looking at the clock.
#   3. a fresh sign-in             driven through a pty using the password at
#                                  $PW_FILE (see lib/op-signin-pty.py for why a
#                                  pty is the only option).
#   4. an interactive prompt       when there is no password file but there is
#                                  a terminal.
#   5. a loud failure              naming the exact command that fixes it.
#
# THE TOKEN IS NOT KEPT IN $HOME. It lives in $XDG_RUNTIME_DIR — tmpfs, 0700,
# and destroyed at logout. A session token is a live credential to the whole
# vault; it should not outlive the login that created it, and it should never
# be sitting on a disk that gets backed up.
#
# WHAT CALLERS GET. `ensure` prints a line to eval, or prints nothing at all
# when a service account is in play:
#
#     line="$(scripts/op-session.sh ensure)" || exit 1
#     [[ -n "$line" ]] && eval "$line"
#
# Capturing op's own `export OP_SESSION_… = …` line rather than a bare --raw
# token means nothing here hardcodes how op names that variable, which has
# changed across major versions.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PTY_HELPER="${OP_PTY_HELPER:-${HERE}/lib/op-signin-pty.py}"

# The account shorthand from `op account list`. OP_ACCOUNT is op's own variable,
# so honouring it keeps this consistent with plain op usage.
ACCOUNT="${OP_ACCOUNT:-my}"

PW_FILE="${OP_PASSWORD_FILE:-$HOME/.config/selfhosted/op-password}"

# Only needed where the account has never been added — a fresh machine, or the
# claude-workspace pod on a new PVC. Two lines: the sign-in address and the
# email on the first, the Secret Key on the second file.
#
#   op-account:      my.1password.com<newline>you@example.org
#   op-secret-key:   A3-XXXXXX-…
#
# `op account add` has --address and --email flags but no --secret-key, so the
# key has to be typed at a prompt like the password does.
ACCOUNT_FILE="${OP_ACCOUNT_FILE:-$HOME/.config/selfhosted/op-account}"
SECRET_KEY_FILE="${OP_SECRET_KEY_FILE:-$HOME/.config/selfhosted/op-secret-key}"

# XDG_RUNTIME_DIR is tmpfs and per-user, which is what systemd user units get.
# The fallback is for contexts without one (cron, some containers) and is
# /dev/shm rather than /tmp deliberately: /tmp is ext4 on this machine, so a
# token cached there would be written to a disk — the same reason secrets.sh
# does all its scratch work in /dev/shm.
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/dev/shm/selfhosted-$(id -u)}"
STATE_DIR="${RUNTIME_DIR}/selfhosted"
TOKEN_FILE="${OP_SESSION_FILE:-${STATE_DIR}/op-session}"

die() { echo "op-session: $*" >&2; exit 1; }
note() { echo "op-session: $*" >&2; }

have_service_account() { [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; }

need_op_binary() {
  command -v op >/dev/null 2>&1 \
    || die "op not found. Install the 1Password CLI (>= 2.0)."
}

# Validate by making op actually talk to the server, in a subshell so a dead
# token never leaks into this shell's environment.
#
# NOT `op whoami`. That answers from local state and SUCCEEDS against a session
# the server has already invalidated — verified the hard way: whoami returned
# the right email while `op read`, `op inject` and `op vault list` all failed
# with "You are not currently signed in" using the same token. A liveness probe
# that lies is worse than none, because op-session then hands out a dead token
# and every caller fails somewhere further downstream with that same misleading
# message.
#
# `op vault list` is the cheapest call that requires a real round-trip. An
# expiry check against the clock would be a guess in the other direction: op
# invalidates sessions for reasons besides time, signing in elsewhere among
# them.
token_line_works() {
  local line="$1"
  [[ -n "$line" ]] || return 1
  ( eval "$line" && op vault list --account "$ACCOUNT" --format json >/dev/null 2>&1 )
}

read_cached() { [[ -f "$TOKEN_FILE" ]] && cat "$TOKEN_FILE" || true; }

write_cached() {
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  ( umask 077; printf '%s\n' "$1" > "$TOKEN_FILE" )
}

# A credential readable by anyone else on the box makes every other precaution
# here decorative.
# Enumerating acceptable modes was wrong in both directions. It rejected 0440,
# which is what a read-only Kubernetes Secret volume necessarily produces once
# fsGroup applies — so the claude-workspace pod would die on a perfectly safe
# file. And it accepted 0400 on a file owned by somebody else, which is not
# safe at all. The property that actually matters is that no OTHER user can
# read it.
# stat -L, not stat: a Kubernetes Secret volume presents each key as a SYMLINK
# into ..data/, and a symlink's own mode is always 0777. Without -L this reports
# 777 for a file that is really 0440 and refuses to start.
check_mode() {
  local f="$1" mode; mode="$(stat -Lc '%a' "$f")"
  (( 8#$mode & 8#007 )) \
    && die "${f} is mode ${mode} — world-readable. Fix: chmod o-rwx ${f}"
  return 0
}

account_known() { op account list 2>/dev/null | grep -q "$ACCOUNT"; }

# A machine that has never run `op account add` has no device registration, so
# there is nothing for a sign-in to attach to. This is the normal state of a
# fresh claude-workspace PVC, and doing it lazily here — rather than in the
# pod's initContainer — means it happens in a container that definitely has
# network, at the moment something actually needs the vault.
ensure_account() {
  account_known && return 0
  [[ -f "$ACCOUNT_FILE" && -f "$SECRET_KEY_FILE" && -f "$PW_FILE" ]] || return 0
  check_mode "$PW_FILE"; check_mode "$SECRET_KEY_FILE"
  local address email
  { read -r address; read -r email; } < "$ACCOUNT_FILE"
  [[ -n "$address" && -n "$email" ]] \
    || die "${ACCOUNT_FILE} should hold the sign-in address on line 1 and the email on line 2"
  note "adding account ${email} at ${address} (first run on this machine)"
  python3 "$PTY_HELPER" --mode add-account --account "$ACCOUNT" \
    --address "$address" --email "$email" \
    --secret-key-file "$SECRET_KEY_FILE" --password-file "$PW_FILE" >/dev/null \
    || die "op account add failed — check ${ACCOUNT_FILE}, ${SECRET_KEY_FILE} and ${PW_FILE}"
}

sign_in() {
  local line
  ensure_account
  if [[ -f "$PW_FILE" ]]; then
    check_mode "$PW_FILE"
    [[ -f "$PTY_HELPER" ]] || die "missing ${PTY_HELPER}"
    line="$(python3 "$PTY_HELPER" --mode signin --account "$ACCOUNT" --password-file "$PW_FILE")" \
      || die "sign-in failed. Check the password in ${PW_FILE}, or run: op signin --account ${ACCOUNT}"
  elif [[ -t 0 ]]; then
    # op prompts on the terminal and prints the export line on stdout, so this
    # capture works even though the prompt is interactive.
    line="$(op signin --account "$ACCOUNT")" || die "sign-in failed"
  else
    die "no session, no password file, and no terminal.
       Write the account password (mode 600) to: ${PW_FILE}
       or sign in once by hand:                  eval \$(op signin --account ${ACCOUNT})"
  fi
  [[ -n "$line" ]] || die "sign-in produced no session token"
  write_cached "$line"
  printf '%s\n' "$line"
}

cmd_ensure() {
  # A service account needs no session at all — print nothing and let op read
  # the token from the environment.
  have_service_account && return 0
  need_op_binary

  local cached; cached="$(read_cached)"
  if token_line_works "$cached"; then printf '%s\n' "$cached"; return 0; fi
  sign_in >/dev/null
  read_cached
}

cmd_login() {
  have_service_account && { note "OP_SERVICE_ACCOUNT_TOKEN is set; nothing to do"; return 0; }
  need_op_binary
  rm -f "$TOKEN_FILE"
  sign_in >/dev/null
  note "signed in as $(op_email)"
}

op_email() {
  local line; line="$(read_cached)"
  ( eval "$line" 2>/dev/null; op whoami --account "$ACCOUNT" --format json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("email","?"))' 2>/dev/null ) \
    || echo '?'
}

cmd_status() {
  if have_service_account; then echo "service account token set (no session needed)"; return 0; fi
  need_op_binary
  local cached; cached="$(read_cached)"
  # Deliberately the same probe `ensure` uses. A status that reported "valid"
  # from a weaker check than the one callers depend on would be actively
  # misleading — which is exactly what it did while this used op whoami.
  if token_line_works "$cached"; then
    echo "session valid   ${TOKEN_FILE}  ($(op_email))"
    return 0
  fi
  echo "no valid session (cache: ${TOKEN_FILE:-none})"
  [[ -f "$PW_FILE" ]] && echo "password file present — 'ensure' will sign in unattended" \
                      || echo "no password file at ${PW_FILE} — 'ensure' needs a terminal"
  return 1
}

cmd_forget() { rm -f "$TOKEN_FILE"; note "cleared ${TOKEN_FILE}"; }

usage() {
  cat <<'EOF'
Usage: scripts/op-session.sh [ensure|login|status|forget]

  ensure   print an eval-able session line, signing in if needed (default)
  login    force a fresh sign-in, discarding any cached session
  status   report whether a usable session exists
  forget   discard the cached session

Set up unattended use once:
  install -m600 /dev/null ~/.config/selfhosted/op-password
  # then write the account password into it, with no trailing spaces
EOF
}

case "${1:-ensure}" in
  ensure) cmd_ensure ;;
  login)  cmd_login ;;
  status) cmd_status ;;
  forget) cmd_forget ;;
  -h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
