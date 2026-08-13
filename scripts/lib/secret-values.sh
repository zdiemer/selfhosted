#!/usr/bin/env bash
# Resolve a chart's secrets into RAM and hand them to helm without ever writing
# them to a disk.
#
# SOURCE THIS FILE; DO NOT EXECUTE IT.
#
#     . "${HERE}/../../scripts/lib/secret-values.sh"
#     sv_load "$HERE" || exit 1
#     helm upgrade --install "$RELEASE" "$HERE" -n "$NS" -f "$VALUES" -f <(sv_fd)
#
# WHY THIS EXISTS
# values.local.yaml used to be materialized next to the chart, so every secret
# in the fleet sat on ext4 between deploys — 26 files on the laptop, and the
# same set unpacked onto the claude-workspace PVC on every pod start. The vault
# was already the source of truth; the files were just a cache nobody needed.
# This is that cache, deleted.
#
# WHY A VARIABLE AND NOT  -f <(op inject -i tpl)  DIRECTLY
# web/talaria/upgrade.sh:101 does exactly that with sops and it is right there,
# one call, one read. It does not generalise, for two independent reasons:
#
#   1. A /dev/fd pipe is readable EXACTLY ONCE. Eight scripts here read their
#      values twice or more — infra/duckdns renders templates/secret.yaml to
#      find Traefik's namespace before it upgrades, infra/traefik has a render()
#      called four times, infra/alloy and infra/k8up parse the file as a
#      pre-flight. The second reader gets EOF and sees an empty values file.
#      Holding the text in a variable means every `<(sv_fd)` is a fresh pipe.
#
#   2. op inject's exit status DIES INSIDE <(). `set -euo pipefail` never sees
#      it, so a failed injection reaches helm as a truncated or empty file
#      rather than as an error. finance/money/upgrade.sh:19 spells out where
#      that ends: a render missing imageCredentials.pat makes helm DELETE
#      secret/money-ghcr, and nothing looks wrong until the next pod start.
#      Against a variable we can check the thing before anyone consumes it.
#
# NOTHING HERE EVER WRITES A SECRET TO A DISK. sv_scratch/sv_file refuse to run
# at all when no tmpfs can be found, rather than falling back to /tmp — a silent
# fallback would leave every script working and quietly undo the entire point.

[[ -n "${_SV_SOURCED:-}" ]] && return 0
_SV_SOURCED=1

SV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Diagnostics go to stderr. sv_fd's stdout is the values document itself, and a
# stray echo there becomes part of the YAML helm parses.
sv_note() { echo "==> $*" >&2; }
sv_fail() { echo "FAIL: $*" >&2; return 1; }

# ---------------------------------------------------------------- the tmpfs
# Assert, never assume. `stat -f -c %T` reports the FILESYSTEM TYPE, which is
# the only thing that actually answers "does this survive a power cut".
# /tmp is last and conditional: it is ext4 on the laptop and a Memory emptyDir
# in the claude-workspace pod, so one rule gives the right answer in both.
_sv_is_tmpfs() {
  [[ -n "$1" && -d "$1" && -w "$1" ]] || return 1
  case "$(stat -f -c %T "$1" 2>/dev/null)" in tmpfs|ramfs) return 0 ;; esac
  return 1
}

_sv_runtime_root() {
  local c
  for c in "${SELFHOSTED_RUNTIME_DIR:-}" "${XDG_RUNTIME_DIR:-}" \
           "/run/user/$(id -u)" /dev/shm /tmp; do
    _sv_is_tmpfs "$c" && { printf '%s\n' "$c"; return 0; }
  done
  return 1
}

# Created at SOURCE time, in the caller's shell — never lazily inside a
# function. Callers invoke sv_scratch via `d="$(sv_scratch)"`, and a command
# substitution is a subshell: a directory recorded there would be invisible to
# the parent's EXIT trap, leaking one tmpfs directory of plaintext per call.
SV_DIR=""
_sv_root="$(_sv_runtime_root)" || {
  echo "FAIL: no tmpfs available (tried \$SELFHOSTED_RUNTIME_DIR, \$XDG_RUNTIME_DIR," >&2
  echo "      /run/user/$(id -u), /dev/shm, /tmp). Refusing to put secrets on a disk." >&2
  return 1 2>/dev/null || exit 1
}
SV_DIR="$(mktemp -d "${_sv_root}/selfhosted.XXXXXX")" || return 1
chmod 700 "$SV_DIR"
unset _sv_root

sv_cleanup() {
  [[ -n "${SV_DIR:-}" && -d "$SV_DIR" ]] && rm -rf "$SV_DIR"
  SV_DIR=""
  # The variables are the other copy. Clearing them shortens the window in
  # which a core dump or a /proc read would find them.
  SV_YAML=""
  unset SV_YAML_MAP 2>/dev/null || true
  declare -gA SV_YAML_MAP 2>/dev/null || true
  return 0
}

# Chain rather than replace. infra/egress-proxy, infra/grafana-dashboards and
# infra/traefik-certs/seed.sh each install their own EXIT trap, and a later
# `trap foo EXIT` silently discards whatever was there — which would leave a
# tmpfs directory of plaintext behind for the rest of the boot. Those scripts
# call sv_trap_add instead of trap.
sv_trap_add() {
  local handler="$1" sig prev
  for sig in EXIT INT TERM; do
    prev="$(trap -p "$sig" | sed -n "s/^trap -- '\(.*\)' ${sig}\$/\1/p")"
    if [[ -n "$prev" && "$prev" != *"$handler"* ]]; then
      trap "${handler}; ${prev}" "$sig"
    elif [[ -z "$prev" ]]; then
      trap "$handler" "$sig"
    fi
  done
}
sv_trap_add sv_cleanup

# ------------------------------------------------------------- primitives
# A 0700 directory inside this run's tmpfs. Replaces every `mktemp -d` that was
# landing on ext4 — infra/traefik-certs/seed.sh:69 held the WILDCARD TLS PRIVATE
# KEY in one of those.
sv_scratch() {
  local d; d="$(mktemp -d "${SV_DIR}/${1:-w}.XXXXXX")" || return 1
  chmod 700 "$d"; printf '%s\n' "$d"
}

# An empty 0600 file inside this run's tmpfs, at a name you choose. Replaces
# `mktemp` — as in infra/grafana-dashboards/upgrade.sh:72, which held a Grafana
# bearer token in /tmp, i.e. on ext4.
sv_file() {
  local f="${SV_DIR}/${1:?sv_file needs a name}"
  ( umask 077; : > "$f" ) || return 1
  printf '%s\n' "$f"
}

# The same thing with a unique name, for call sites inside a loop where a fixed
# name would have two live users at once. This is the drop-in for a bare
# `mktemp`, which would otherwise put the file on whatever /tmp happens to be.
sv_mktemp() {
  local f
  f="$(mktemp "${SV_DIR}/${1:-t}.XXXXXX")" || return 1
  chmod 600 "$f"
  printf '%s\n' "$f"
}

# ------------------------------------------------------------- 1Password
# Sessions are obtained, not assumed — the same indirection scripts/secrets.sh
# uses, which is what lets a deploy work from a timer with no terminal.
#
# The eval lands in this function's caller, not globally exported: op reads
# OP_SESSION_* from the environment of the process it starts, and exporting it
# for the whole run would hand a live vault token to helm, kubectl and every
# other child for the rest of the script.
_sv_session() {
  local helper="${SV_ROOT}/scripts/op-session.sh" line=""
  [[ -x "$helper" ]] || return 0
  line="$("$helper" ensure)" || return 1
  [[ -n "$line" ]] && eval "$line"
  return 0
}

declare -gA SV_YAML_MAP=()
SV_YAML=""

# _sv_inject <template> -> sets _SV_OUT, returns nonzero on failure
#
# Sets a variable rather than printing, because the caller would otherwise wrap
# it in another command substitution and strip the trailing newline a second
# time — the exact byte the `printf x` dance below exists to preserve.
_sv_inject() {
  local tpl="$1" out
  _SV_OUT=""
  command -v op >/dev/null 2>&1 || { sv_fail "op not found; cannot resolve ${tpl}"; return 1; }
  _sv_session || { sv_fail "no 1Password session — run: ${SV_ROOT}/scripts/op-session.sh login"; return 1; }

  # The trailing 'x' is not a typo. Command substitution strips EVERY trailing
  # newline, so a plain out="$(op inject ...)" hands helm a document one byte
  # short of what op produced and of what scripts/secrets.sh writes. Nothing
  # about helm cares, but "the helper's output is byte-identical to op inject"
  # is the invariant every gate in this migration is checked against, and an
  # off-by-one-newline turns those comparisons into noise you learn to ignore.
  # `exit $rc` is what keeps the || below testing op inject rather than printf,
  # which would always succeed and make every failure look like a success.
  out="$(op inject -i "$tpl" 2>/dev/null; rc=$?; printf x; exit $rc)" \
    || { sv_fail "op inject failed for ${tpl}"; return 1; }
  out="${out%x}"

  # The same three assertions scripts/secrets.sh makes in inject_to() before it
  # will replace a working file. They matter MORE here, because there is no
  # longer a working file to fall back to.
  [[ -n "$out" ]] || { sv_fail "${tpl} resolved to nothing"; return 1; }
  # op inject fails loudly on an unresolvable reference, but a vault field that
  # EXISTS AND IS EMPTY resolves to the empty string and renders a Secret with a
  # blank value — which deploys perfectly and breaks at runtime.
  [[ "$out" != *'{{ op://'* ]] || { sv_fail "${tpl} left an unresolved op:// reference"; return 1; }
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$out" | python3 -c 'import sys,yaml; yaml.safe_load(sys.stdin)' 2>/dev/null \
      || { sv_fail "${tpl} produced invalid YAML (op inject is a raw text substitution)"; return 1; }
  fi
  _SV_OUT="$out"
}

# ------------------------------------------------------------- entry points
#
# sv_load <chart-dir> [stem]
#   Resolves <chart-dir>/<stem>.tpl.yaml into memory. Returns 0 with nothing
#   loaded when the chart has no template at all (a chart with no secrets, e.g.
#   web/old-diemer-codes) so call sites need no conditional. Returns 1 when a
#   template exists and could not be resolved — that is a hard failure.
#
#   The default stem is values.local. web/apartment-watch also carries
#   criteria.yaml, hence the parameter:  sv_load "$HERE" criteria
sv_load() {
  local dir="${1:?sv_load needs a chart directory}" stem="${2:-values.local}"
  local tpl="${dir}/${stem}.tpl.yaml" out

  if [[ ! -f "$tpl" ]]; then
    SV_YAML_MAP["$stem"]=""
    [[ "$stem" == "values.local" ]] && SV_YAML=""
    return 0
  fi

  sv_note "resolving ${stem}.yaml from 1Password (memory only, this run)"
  _sv_inject "$tpl" || return 1
  SV_YAML_MAP["$stem"]="$_SV_OUT"
  [[ "$stem" == "values.local" ]] && SV_YAML="$_SV_OUT"
  _SV_OUT=""
  return 0
}

# Same, but a failure is survivable. ONE caller: infra/crowdsec, whose
# upgrade.sh has always treated a missing values.local.yaml as degraded rather
# than fatal — without it the default profile from values.yaml applies, which
# (per commit 727d259) "bans exactly the same things and notifies nobody, so a
# fresh clone is still correct and still enforcing, just silent". Turning that
# into a hard failure would take the cluster's enforcement layer down over a
# notifier. Explicit verb rather than `|| true` at the call site, so nobody
# later "fixes" the stray `|| true` and reintroduces the outage.
sv_load_optional() {
  sv_load "$@" && return 0
  sv_note "continuing without ${2:-values.local}.yaml"
  return 1
}

# sv_fd [stem] — the document, on stdout. Used as -f <(sv_fd).
#
# Every call site writes its own <(sv_fd), rather than sharing one fd through a
# VALUE_ARGS array, precisely so that a script reading its values twice gets two
# live pipes instead of one exhausted one.
sv_fd() { printf '%s' "${SV_YAML_MAP[${1:-values.local}]-}"; }

# sv_has [stem] — did anything load? For the `[[ -f "$LOCAL_VALUES" ]]` guards
# that used to gate whether -f was passed at all.
sv_has() { [[ -n "${SV_YAML_MAP[${1:-values.local}]-}" ]]; }
