#!/usr/bin/env bash
# Move per-project secrets between 1Password, this checkout, the cluster, and
# the NAS.
#
# WHY THIS EXISTS
# Every secret in this repo used to live in a gitignored values.local.yaml on
# one laptop. Losing that machine lost the DuckDNS token (every cert in the
# cluster), both Cloudflare tunnel tokens, Authelia's OIDC signing key and user
# database, and fifteen other projects' credentials. There was no second copy.
#
# THE CONTRACT HAS NOT CHANGED. values.local.yaml is still the only thing helm
# reads. This script materializes those files; it never replaces them, and no
# deploy path depends on it. A standalone clone of an app repo has no scripts/
# directory and needs none — every values.local.tpl.yaml carries the one-liner
# that reproduces it:
#
#     op inject -i values.local.tpl.yaml -o values.local.yaml -f
#
# THREE PLACES SECRETS LIVE
#   1Password   source of truth, multi-machine, reachable from anywhere
#   the cluster a bundle Secret the claude-workspace pod pulls for itself
#   the NAS     an age-encrypted archive, for when 1Password itself is gone
#
# Nothing secret is ever committed. The tracked values.local.tpl.yaml files hold
# only op:// references — a vault name, a chart path, and a values path, all of
# which are already public in the repo tree.

set -euo pipefail

# WHERE THE CHECKOUT IS, IF THERE IS ONE.
#
# Bundled as a single executable on PATH (scripts/build-secrets-cli.sh), this
# runs with no repo around it at all. Most of what it does is vault work and
# needs none: show, edit, check, backup and restore only ever talk to 1Password
# and the NAS. Two verbs genuinely cannot work without a checkout — `new` writes
# a template into a git repo, and `verify` renders charts — and they say so
# rather than half-working.
#
# $SELFHOSTED_ROOT wins; then the tree this script sits in, if it is one; then
# the conventional clone. HAVE_REPO is the honest answer, not a guess.
_looks_like_repo() { [[ -n "$1" && -f "$1/scripts/secrets.sh" && -d "$1/scripts/lib" ]]; }
if [[ -n "${SELFHOSTED_ROOT:-}" ]]; then
  # An explicit override that is wrong must fail, not fall through to a
  # different tree — silently using a checkout the caller did not name is how
  # you edit the wrong fleet's secrets.
  _looks_like_repo "$SELFHOSTED_ROOT" \
    || { echo "SELFHOSTED_ROOT=${SELFHOSTED_ROOT} is not a selfhosted checkout" >&2; exit 1; }
  ROOT="$SELFHOSTED_ROOT"
elif _looks_like_repo "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
elif _looks_like_repo "$HOME/Code/selfhosted"; then
  ROOT="$HOME/Code/selfhosted"
elif _looks_like_repo "$HOME/code/selfhosted"; then
  # The laptops clone to ~/Code, the claude-workspace pod to ~/code — the same
  # split EXTERNAL_BASE already accounts for below. Missing it here is not a
  # cosmetic difference: the bundled CLI lives on PATH, so BASH_SOURCE points at
  # /usr/local/bin and this fallback is the only thing that finds the checkout.
  # Without it `secrets check` ran in the pod against ROOT="" and called every
  # chart UNRESOLVABLE.
  ROOT="$HOME/code/selfhosted"
else
  ROOT=""
fi
HAVE_REPO=$([[ -n "$ROOT" ]] && echo 1 || echo 0)

# Refuse clearly rather than producing an empty result from an empty tree.
need_repo() {
  [[ $HAVE_REPO -eq 1 ]] && return 0
  die "'$1' needs the selfhosted checkout (it $2).
       Point at one:  SELFHOSTED_ROOT=/path/to/selfhosted secrets $1 ...
       Everything that only touches the vault — show, edit, check, backup,
       restore — works without it."
}

VAULT="${VAULT:-homelab}"

# Cluster relay (claude-workspace). The pod runs as a cluster-admin SA, so it
# can already read every Secret in the cluster — including the rendered chart
# Secrets that carry most of these same values. Putting the bundle in a Secret
# therefore grants it nothing it did not already have, and buys a pull-based
# path: the pod refreshes itself after a restart without this laptop being up.
POD_DEPLOY="${POD_DEPLOY:-claude-workspace}"
POD_REPO="${POD_REPO:-/home/node/code/selfhosted}"

# NAS backup. A dedicated share — deliberately NOT the games dataset, which
# games/romm mounts into the cluster read-only.
NAS_SHARE="${NAS_SHARE:-//192.168.4.36/backups}"
NAS_DIR="${NAS_DIR:-selfhosted-secrets}"
NAS_CREDS="${NAS_CREDS:-$HOME/.config/selfhosted/nas-creds}"
AGE_KEY="${AGE_KEY:-$HOME/.config/selfhosted/backup-age.key}"
AGE_RECIPIENTS="${AGE_RECIPIENTS:-${ROOT:-$HOME/.config/selfhosted}/scripts/backup-recipients.txt}"
PAPER_MARKER="${PAPER_MARKER:-$HOME/.config/selfhosted/.paper-key-confirmed}"

# Secret files that are not values.local.yaml. The mechanism is generic; its
# only user was web/apartment-watch, which read criteria off disk via .Files.Get
# in templates/configmap.yaml and so needed it present before helm ran, exactly
# like a -f file. That chart was deprecated 2026-08-13 and its vault items were
# archived, so this is empty — the criteria.tpl.yaml handling below is kept for
# the next chart that owns a second secret file.
EXTRA_FILES=()

# infra/coredns-config/values.local.yaml is NOT a secret and must never be
# managed here. Its *presence* is what enables the cluster-wide DNS query log;
# its own .example says the tracked default must stay false and that deleting
# the file is how you close the window. Materializing it would silently reopen
# cluster DNS logging and restart CoreDNS.
#
# web/apartment-watch was deprecated 2026-08-13 and both its vault items were
# archived, but its two .tpl.yaml files are kept in the tree as the record of
# what the chart needed. Excluding them here is what stops discovery from
# looking for items that no longer exist and reporting the chart broken.
EXCLUDE_FILES=(
  "infra/coredns-config/values.local.yaml"
  "web/apartment-watch/values.local.yaml"
  "web/apartment-watch/criteria.yaml"
)

# Charts whose GATE2 render needs inputs beyond values.yaml + the secret, so the
# generic render in cmd_verify cannot stand them up and a plain failure would be
# a lie. Same idea as TEMPLATE_SKIP in scripts/ci-lint-charts.sh, and the same
# rule applies: this list is for charts the GATE cannot express, never for
# charts that are actually broken.
#
#   infra/democratic-csi   renders twice, once per driver, each needing its own
#                          -f values-<driver>.yaml; without one the chart fails
#                          on `csiDriver.name is required`.
#
# (web/apartment-watch was the second entry until it was deprecated 2026-08-13.
# It is excluded from discovery entirely now, so it never reaches a GATE.)
GATE2_SKIP=(
  "infra/democratic-csi"
)
gate2_skipped() {
  local d="$1" s
  for s in "${GATE2_SKIP[@]}"; do [[ "$d" == "$s" ]] && return 0; done
  return 1
}

# Apps whose charts live in their own repo and deploy from a standalone clone
# (README §Conventions). Their secrets are outside this checkout entirely.
#
# THE STANDALONE CLONE IS THE SOURCE. Both copies of these files exist on this
# machine — ~/Code/gamedex/values.local.yaml is what deploys, and
# games/gamedex/values.local.yaml is a submodule checkout of the same repo — and
# nothing kept them equal. Two sources for one vault item is a silent-divergence
# machine, so the submodule copy is demoted to a materialize-on-demand artifact:
# excluded from discovery here, still reproducible by the op inject one-liner
# its own template carries.
#
# Each entry is  <submodule chart path>:<path under EXTERNAL_BASE>. The left
# side is the LOGICAL identity — item titles are derived from it, so they stay
# exactly what the migration created (games/gamedex -> games-gamedex) no matter
# which copy is being read. whatnowgg keeps its chart at deploy/chart/ rather
# than at the repo root; the mapping absorbs that without a special case.
EXTERNAL_MAP=(
  "games/gamedex:gamedex"
  "finance/money:money"
  "discord/smitele-bot:smitele-bot"
  "infra/sms-relay:sms-relay"
  "web/whatnowgg/deploy/chart:whatnowgg/deploy/chart"
)
# web/talaria is deliberately absent: talaria keeps its secrets sops-encrypted
# in git (.sops.yaml, helm/talaria/secrets.*.yaml), so it has no
# values.local.yaml to manage and would otherwise be reported NOT MIGRATED
# forever. Its sops age key lives in the vault as its own item instead.

# Kept as the flat list the bundle format uses (external/<name>/...), so the
# on-disk layout the pod already understands does not change.
EXTERNAL_REPOS=()
for _pair in "${EXTERNAL_MAP[@]}"; do EXTERNAL_REPOS+=("${_pair#*:}"); done
unset _pair

# The laptop clones under ~/Code, the claude-workspace pod under ~/code. Pick
# whichever exists so the same command works in both without a flag.
if [[ -n "${EXTERNAL_BASE:-}" ]]; then :
elif [[ -d "$HOME/Code" ]]; then EXTERNAL_BASE="$HOME/Code"
elif [[ -d "$HOME/code" ]]; then EXTERNAL_BASE="$HOME/code"
else EXTERNAL_BASE="$HOME/Code"
fi

# Submodule paths whose secrets are managed from the standalone clone instead.
#
# NOT simply "every submodule", and the requirement is the CLONE EXISTING, not
# the mapping existing. A submodule with no clone under ~/Code deploys from the
# checkout in this repo, so suppressing it would silently drop a managed secret
# — infra/sms-relay was exactly that until its clone was made, and the naive
# rule dropped it on the first run. Falling back to the checkout means a machine
# that has cloned only some of these repos still manages all of them.
MIRROR_PREFIXES=()
if [[ -f "${ROOT}/.gitmodules" ]] && command -v git >/dev/null 2>&1; then
  while IFS= read -r _sub; do
    [[ -n "$_sub" ]] || continue
    for _pair in "${EXTERNAL_MAP[@]}"; do
      _logical="${_pair%%:*}"
      [[ "$_logical" == "$_sub" || "$_logical" == "$_sub"/* ]] || continue
      [[ -d "${EXTERNAL_BASE}/${_pair#*:}" ]] || continue
      MIRROR_PREFIXES+=("$_sub"); break
    done
  done < <(
    git -C "$ROOT" config -f .gitmodules --get-regexp '^submodule\..*\.path$' 2>/dev/null \
      | awk '{print $2}'
  )
  unset _sub _pair _logical
fi

# What the last successful sync agreed on, per item field: a sha256 of the
# content both sides held at that moment. Comparing today's two sides against a
# remembered third point is the only way to tell "the vault moved" from "the
# file moved" from "both moved" — mtime alone cannot, which is why cmd_status
# hedges its wording and why nothing before this wrote anything automatically.
SYNC_STATE_DIR="${SYNC_STATE_DIR:-$HOME/.config/selfhosted/sync-state}"

DRY_RUN=0
FORCE=0
# External clones are in scope by default now. They are where secrets actually
# change; leaving them opt-in is what left ~/Code/whatnowgg unmanaged entirely.
INCLUDE_EXTERNAL=1
NO_BACKUP=0
VERIFY_ONLY=0
ASSUME_YES=0
FILES_ONLY=0
KEEP="${KEEP:-20}"
ITEM_OVERRIDE=""
PUSH_FROM=""
FROM_STDIN=0
REVEAL=0

die() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "==> $*"; }
skip() { echo "    skip: $*"; }

# ---------------------------------------------------------------- discovery

is_excluded() {
  local f="$1" e
  for e in "${EXCLUDE_FILES[@]}"; do [[ "$f" == "$e" ]] && return 0; done
  return 1
}

# True for a path inside any submodule. Prefix match on the submodule root, so
# it catches nested charts (web/whatnowgg/deploy/chart) as well as top-level
# ones, and needs no update when a submodule is added.
is_mirror() {
  local f="$1" p
  [[ ${#MIRROR_PREFIXES[@]} -eq 0 ]] && return 1
  for p in "${MIRROR_PREFIXES[@]}"; do
    [[ "$f" == "$p" || "$f" == "$p"/* ]] && return 0
  done
  return 1
}

# The one place the checkout is walked, and the one place ROOT is allowed to be
# empty. `cd ""` is a silent no-op in bash rather than an error, so without this
# guard a no-checkout run walks whatever the caller's cwd happens to be and emits
# paths like /auth/authelia/values.local.tpl.yaml — which resolve to nothing and
# made `secrets check` call all 30 charts UNRESOLVABLE from inside the tree, and
# exit 0 having checked only the four external clones from anywhere else. Both
# answers were wrong, and the second one was wrong quietly.
repo_scan() {
  [[ $HAVE_REPO -eq 1 ]] || return 0
  (cd "$ROOT" && find "$@" -not -path './.git/*' | sort)
}

# Every managed secret file that currently exists, repo-relative. Mirrors are
# omitted: their real copy is discovered by discover_pairs from ~/Code.
discover_local() {
  local f
  while IFS= read -r f; do
    f="${f#./}"
    is_excluded "$f" && continue
    is_mirror "$f" && continue
    printf '%s\n' "$f"
  done < <(repo_scan . -name values.local.yaml)
  for f in "${EXTRA_FILES[@]}"; do
    [[ -f "$ROOT/$f" ]] && printf '%s\n' "$f"
  done
  # Explicit: the loop's status is the last [[ -f ]], so a missing optional
  # extra file would otherwise make this function "fail".
  return 0
}

# Every migrated chart, as three tab-separated columns:
#
#   logical      repo-relative identity — what item titles and messages use
#   tpl          ABSOLUTE path to the template
#   out          ABSOLUTE path to the file it materializes
#
# Logical and real diverge only for the standalone clones, where the identity
# stays the submodule path (so op://homelab/games-gamedex/... keeps working)
# while the bytes live under ~/Code. Everything downstream operates on absolute
# paths, which is what lets one code path serve both.
discover_pairs() {
  local t out logical dir ext etpl eout
  while IFS= read -r t; do
    t="${t#./}"
    out="$(tpl_output "$t")"
    is_excluded "$out" && continue
    is_mirror "$out" && continue
    printf '%s\t%s\t%s\n' "$out" "${ROOT}/${t}" "${ROOT}/${out}"
  done < <(repo_scan . \( -name 'values.local.tpl.yaml' -o -name 'criteria.tpl.yaml' \))

  [[ $INCLUDE_EXTERNAL -eq 1 ]] || return 0
  local pair
  for pair in "${EXTERNAL_MAP[@]}"; do
    logical="${pair%%:*}"; ext="${EXTERNAL_BASE}/${pair#*:}"
    [[ -d "$ext" ]] || continue
    etpl="${ext}/values.local.tpl.yaml"
    eout="${ext}/values.local.yaml"
    [[ -f "$etpl" ]] || continue
    printf '%s\t%s\t%s\n' "${logical}/values.local.yaml" "$etpl" "$eout"
  done
  return 0
}

# Kept for readability at call sites that only want "tpl<TAB>out" repo-relative.
discover_tpl() {
  local t out
  while IFS= read -r t; do
    t="${t#./}"
    out="$(tpl_output "$t")"
    is_excluded "$out" && continue
    is_mirror "$out" && continue
    printf '%s\t%s\n' "$t" "$out"
  done < <(repo_scan . \( -name 'values.local.tpl.yaml' -o -name 'criteria.tpl.yaml' \))
}

# values.local.tpl.yaml -> values.local.yaml ; criteria.tpl.yaml -> criteria.yaml
tpl_output() {
  local t="$1" d b
  d="$(dirname "$t")"; b="$(basename "$t")"
  case "$b" in
    values.local.tpl.yaml) b="values.local.yaml" ;;
    *.tpl.yaml)            b="${b%.tpl.yaml}.yaml" ;;
    *) die "unrecognised template name: $t" ;;
  esac
  [[ "$d" == "." ]] && printf '%s\n' "$b" || printf '%s/%s\n' "$d" "$b"
}

# auth/authelia/values.local.yaml       -> auth-authelia
# web/apartment-watch/criteria.yaml     -> web-apartment-watch-criteria
#
# `/` becomes `-` because op:// parses `/` as a separator, so a title containing
# one is unaddressable.
#
# ONE FILE = ONE ITEM, deliberately. Two `op item edit` limitations make
# multi-file items unworkable (both verified against op 2.38):
#   - JSON on stdin only UPDATES existing fields; a new field is silently
#     ignored and the command still exits 0.
#   - The `label[type]=value` assignment form splits the label on `.`, treating
#     the leading part as a section — 'criteria.yaml[text]=…' lands in a field
#     called `yaml`.
# Creating an item with all its fields at once via --template works fine, so
# per-key mode (Phase 2) stays viable; only incremental field addition is out.
item_title() {
  local f="$1" dir base
  dir="$(dirname "$f")"; base="$(basename "$f")"
  [[ "$dir" == "." ]] && dir="root"
  dir="${dir//\//-}"
  if [[ "$base" == "values.local.yaml" ]]; then
    printf '%s\n' "$dir"
  else
    printf '%s-%s\n' "$dir" "${base%.yaml}"
  fi
}

field_label() { basename "$1"; }

# ---------------------------------------------------------------- op helpers

# Sessions are obtained, not assumed. scripts/op-session.sh knows the whole
# story (cached token -> pty sign-in -> interactive -> loud failure); everything
# here just asks it for a line to eval. That one indirection is what lets every
# verb below run from a systemd timer with no terminal attached.
#
# Done once, here, rather than at each op call site: need_op already gates every
# command, and the session lands in this shell's environment where op finds it.
OP_SESSION_READY=0

# Obtain a session, returning non-zero rather than exiting. Split out from
# need_op because cmd_backup has a real degraded mode — a files-only archive —
# and needs to ASK for a session without a failure taking the whole backup down.
op_session_ready() {
  [[ $OP_SESSION_READY -eq 1 ]] && return 0
  command -v op >/dev/null 2>&1 || return 1
  local helper="${OP_SESSION_SH:-${ROOT}/scripts/op-session.sh}" line=""
  if [[ -x "$helper" ]]; then
    line="$("$helper" ensure)" || return 1
    [[ -n "$line" ]] && eval "$line"
  fi
  # `op vault list`, not `op whoami`: whoami answers from local state and
  # succeeds against a session the server has already invalidated, so it would
  # wave through a dead token and let every verb below fail one layer deeper
  # with "UNRESOLVABLE" — which reads like a broken template, not an expired
  # session. Observed exactly that: 24 of 24 charts unresolvable while whoami
  # cheerfully reported the right email.
  op vault list --format json >/dev/null 2>&1 || return 1
  OP_SESSION_READY=1
}

need_op() {
  op_session_ready && return 0
  command -v op >/dev/null 2>&1 \
    || die "op not found. Install the 1Password CLI (>= 2.0)."
  die "no 1Password session — see the message above, or run: ${OP_SESSION_SH:-${ROOT}/scripts/op-session.sh} login"
}

# Materialize one template. Four guards, each covering a distinct failure, and
# an atomic rename so a bad run can never truncate a working file.
#
# op inject fails loudly on an unresolvable reference — non-zero exit, explicit
# message, nothing written. It does NOT substitute an empty string. The one way
# it can still produce emptiness is a vault field that exists and is empty,
# which is why only non-empty secrets get a ref (optional-empty values stay as
# literals in the template) and why `check` asserts non-emptiness.
#
# Paths are ABSOLUTE. That is what lets the standalone clones under ~/Code go
# through exactly the same code as the charts in this repo.
inject_to() {
  local tpl="$1" out="$2" tmp
  # The temp file sits in the TARGET directory, not /dev/shm, so the final mv is
  # a same-filesystem rename and therefore atomic. A bad run can never leave a
  # half-written values.local.yaml where a working one used to be.
  tmp="$(mktemp "$(dirname "$out")/.secrets.XXXXXX")"
  chmod 600 "$tmp"

  # No RETURN trap here — see scratch() below for why they misfire. Explicit
  # cleanup on every failure path instead.
  _fail() { rm -f "$tmp"; echo "FAIL: $1" >&2; return 1; }

  op inject -i "$tpl" -o "$tmp" -f >/dev/null 2>&1 \
    || { _fail "op inject failed for ${tpl}"; return 1; }
  if grep -q '{{ *op://' "$tmp"; then
    _fail "${tpl} left an unresolved reference"; return 1
  fi
  python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$tmp" 2>/dev/null \
    || { _fail "${tpl} produced invalid YAML (op inject is not YAML-aware)"; return 1; }

  mv "$tmp" "$out"
  chmod 600 "$out"
}

# Same, but to a caller-supplied path in tmpfs — for comparisons that must never
# touch the real file.
inject_probe() {
  local tpl="$1" out="$2"
  op inject -i "$tpl" -o "$out" -f >/dev/null 2>&1 || return 1
  grep -q '{{ *op://' "$out" && return 1
  python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$out" 2>/dev/null || return 1
  return 0
}

# ------------------------------------------------------------ sync markers
#
# The sha256 both sides agreed on at the last successful sync. Without this
# third point of reference, "local differs from the vault" is all you can know,
# and choosing a direction from mtime is a guess — fine for a report, not fine
# for something that overwrites a credential.
# Keyed by the vault coordinates (item title + field label) rather than by path,
# so the standalone clone and the submodule checkout of the same chart can never
# end up with two different opinions about what was last agreed.
marker_path_tl() { printf '%s/%s.%s\n' "$SYNC_STATE_DIR" "$1" "$2"; }
marker_path()    { marker_path_tl "$(item_title "$1")" "$(field_label "$1")"; }
marker_read()    { local m; m="$(marker_path "$1")"; [[ -f "$m" ]] && cat "$m" || true; }
marker_write_tl() {
  local m; m="$(marker_path_tl "$1" "$2")"
  mkdir -p "$SYNC_STATE_DIR"; chmod 700 "$SYNC_STATE_DIR"
  # The hash of a secret is not a secret, but there is no reason to widen it.
  ( umask 077; sha256sum < "$3" | cut -d' ' -f1 > "$m" )
}
marker_write() { marker_write_tl "$(item_title "$1")" "$(field_label "$1")" "$2"; }
sha_of() { sha256sum < "$1" | cut -d' ' -f1; }

# One scratch root for the whole run, removed on any exit.
#
# Deliberately NOT a per-function `trap ... RETURN`: RETURN traps are not
# function-local, so they fire again in the caller — where the trap's variable
# is out of scope. Under `set -u` that aborts the script *after* the work is
# done and leaves a /dev/shm directory full of plaintext secrets behind.
#
# /dev/shm is tmpfs; / is ext4. Decrypted material must never touch a disk.
SCRATCH=""
cleanup_scratch() {
  # $_SC_DIR belongs to the bundled CLI (scripts/build-secrets-cli.sh), which
  # extracts op-session.sh and the pty helper to tmpfs before this script runs.
  # It cannot clean up after itself: the `trap ... EXIT` below REPLACES any trap
  # already installed, so the bundle's own cleanup never fires and every
  # invocation left an extract directory behind. Removing it here is the
  # coupling made explicit — the same trap-clobbering that sv_trap_add exists to
  # avoid in scripts/lib/secret-values.sh.
  [[ -n "${_SC_DIR:-}" && -d "${_SC_DIR:-}" ]] && rm -rf "$_SC_DIR"
  [[ -n "${SCRATCH:-}" && -d "${SCRATCH:-}" ]] && rm -rf "$SCRATCH"
  return 0
}
trap cleanup_scratch EXIT INT TERM

# The root is created once by the dispatcher, in the PARENT shell. It cannot be
# created lazily inside scratch(): every caller uses `tmp="$(scratch)"`, and a
# command substitution is a subshell, so the assignment to SCRATCH would be
# discarded — leaving the EXIT trap with nothing to clean and one abandoned
# tmpfs directory of plaintext secrets per call.
scratch() { mktemp -d "$SCRATCH/w.XXXXXX"; }

# ---------------------------------------------------------------- commands

cmd_check() {
  need_op
  op vault get "$VAULT" >/dev/null 2>&1 || die "vault '${VAULT}' not found (op vault create ${VAULT})"
  note "vault ${VAULT} reachable as $(op whoami --format json | jq -r .email 2>/dev/null || echo '?')"

  # Without a checkout this can only reach the external clones, which is four
  # charts out of thirty-four. That is a legitimate way to run it by hand and a
  # useless thing to put on a timer, so it says so rather than exiting 0 and
  # letting the count go unnoticed.
  [[ $HAVE_REPO -eq 1 ]] \
    || note "no selfhosted checkout found — only the standalone clones under ${EXTERNAL_BASE} are checked"

  local tmp rc=0 n=0; tmp="$(scratch)"
  local logical tpl out ref empty
  while IFS=$'\t' read -r logical tpl out; do
    [[ -n "$logical" ]] || continue
    n=$((n + 1))
    if ! inject_probe "$tpl" "$tmp/probe"; then
      echo "  UNRESOLVABLE  $logical"; rc=1; continue
    fi
    # A ref that resolves to empty is the one way op inject can reintroduce the
    # silent-empty-secret failure this repo has been bitten by before.
    empty=0
    while IFS= read -r ref; do
      [[ -n "$ref" ]] || continue
      [[ -n "$(op read "$ref" 2>/dev/null)" ]] || { echo "  EMPTY FIELD   $ref"; empty=1; }
    done < <(grep -oE 'op://[^ }"]+' "$tpl" | sort -u)
    [[ $empty -eq 1 ]] && rc=1 || echo "  ok            $logical"
  done < <(discover_pairs)
  rm -f "$tmp/probe"
  # The journal only keeps the last few lines for the failure text, so the count
  # is what tells you a "passing" run actually looked at everything.
  note "${n} chart(s) checked"
  return $rc
}

# Decide what a single managed file needs, against the vault and against the
# marker left by the last successful sync. Prints one verdict word and leaves
# the vault's bytes at $probe when it managed to resolve them.
#
#   in-sync | not-materialized | UNRESOLVABLE | pull | push | conflict | unknown
#
# "unknown" is drift with no marker to arbitrate it — the honest answer before
# either side has ever been reconciled. It is never acted on automatically.
classify_one() {
  local logical="$1" tpl="$2" out="$3" probe="$4"
  # Retry once. `op inject` reaches the network, and a single transient failure
  # is not evidence that a template is broken — but it looks identical to one.
  # Observed in practice: one chart out of 24 failed a probe on a run where the
  # other 23 succeeded, then resolved fine on the next two runs. Without this,
  # every such blip costs an SMS and teaches you to ignore the alert.
  if ! inject_probe "$tpl" "$probe"; then
    sleep 2
    inject_probe "$tpl" "$probe" || { echo "UNRESOLVABLE"; return 0; }
  fi
  [[ -f "$out" ]] || { echo "not-materialized"; return 0; }
  cmp -s "$out" "$probe" && { echo "in-sync"; return 0; }

  local marker local_sha vault_sha
  marker="$(marker_read "$logical")"
  [[ -n "$marker" ]] || { echo "unknown"; return 0; }
  local_sha="$(sha_of "$out")"; vault_sha="$(sha_of "$probe")"
  [[ "$local_sha" == "$marker" ]] && { echo "pull";  return 0; }   # only the vault moved
  [[ "$vault_sha" == "$marker" ]] && { echo "push";  return 0; }   # only the file moved
  echo "conflict"                                                  # both moved
}

status_label() {
  case "$1" in
    in-sync)          echo "in-sync" ;;
    not-materialized) echo "not-materialized" ;;
    UNRESOLVABLE)     echo "UNRESOLVABLE" ;;
    pull)             echo "DRIFT (vault newer — sync will pull)" ;;
    push)             echo "DRIFT (local newer — sync will push)" ;;
    conflict)         echo "CONFLICT (both sides changed — resolve by hand)" ;;
    unknown)          echo "DRIFT (no sync marker — push or pull once to settle it)" ;;
    *)                echo "$1" ;;
  esac
}

cmd_status() {
  need_op
  local tmp; tmp="$(scratch)"
  printf '%-46s %s\n' "CHART" "STATE"
  local logical tpl out verdict
  while IFS=$'\t' read -r logical tpl out; do
    [[ -n "$logical" ]] || continue
    verdict="$(classify_one "$logical" "$tpl" "$out" "$tmp/probe")"
    printf '%-46s %s\n' "$logical" "$(status_label "$verdict")"
    rm -f "$tmp/probe"
  done < <(discover_pairs)

  # Files that exist but have no template yet — i.e. not migrated, so not backed
  # up by 1Password. This is the number that matters during the migration.
  local f pending=0
  while IFS= read -r f; do
    local dir base tpl2
    dir="$(dirname "$f")"; base="$(basename "$f")"
    case "$base" in
      values.local.yaml) tpl2="$dir/values.local.tpl.yaml" ;;
      *)                 tpl2="$dir/${base%.yaml}.tpl.yaml" ;;
    esac
    [[ -f "${ROOT}/${tpl2}" ]] || { printf '%-46s %s\n' "$f" "NOT MIGRATED"; pending=$((pending+1)); }
  done < <(discover_local)

  # Same question for the standalone clones. discover_pairs only yields charts
  # that HAVE a template, so a clone whose secret was never imported would
  # otherwise be invisible here — which is precisely the state this whole change
  # exists to stop being possible.
  if [[ $INCLUDE_EXTERNAL -eq 1 ]]; then
    local pair ext
    for pair in "${EXTERNAL_MAP[@]}"; do
      ext="${EXTERNAL_BASE}/${pair#*:}"
      [[ -f "${ext}/values.local.yaml" ]] || continue
      [[ -f "${ext}/values.local.tpl.yaml" ]] && continue
      printf '%-46s %s\n' "${pair%%:*}/values.local.yaml" "NOT MIGRATED (${ext})"
      pending=$((pending+1))
    done
  fi

  [[ $pending -gt 0 ]] && echo && echo "${pending} file(s) still exist only on this machine."
  return 0
}

cmd_pull() {
  need_repo pull "materializes files into a checkout"
  need_op
  local tmp; tmp="$(scratch)"
  local logical tpl out n=0
  while IFS=$'\t' read -r logical tpl out; do
    [[ -n "$logical" ]] || continue
    if [[ $# -gt 0 ]] && ! printf '%s\n' "$@" | grep -qF "$(dirname "$logical")"; then continue; fi
    if [[ -f "$out" && $FORCE -eq 0 ]]; then
      if inject_probe "$tpl" "$tmp/probe" && ! cmp -s "$out" "$tmp/probe"; then
        skip "$logical differs from the vault — inspect with 'status', then pull --force or push"
        rm -f "$tmp/probe"; continue
      fi
      rm -f "$tmp/probe"
    fi
    if [[ $DRY_RUN -eq 1 ]]; then echo "    would materialize $logical"; n=$((n+1)); continue; fi
    # Record what both sides now agree on, so the next sync can tell which side
    # moved instead of guessing. A pull that did not update the marker would
    # make its own result look like local drift on the following run.
    inject_to "$tpl" "$out" && { marker_write "$logical" "$out"; echo "    $logical"; n=$((n+1)); }
  done < <(discover_pairs)
  note "$n file(s)"
}

# One-time migration: store an existing file in 1Password verbatim, then write
# the template that reproduces it. Whole-file is lossless and mechanical, so it
# carries near-zero risk while delivering the entire recoverability benefit.
# Per-key conversion is where the judgment calls live; do it later, per chart.
# ---------------------------------------------------------------- authoring
#
# WHY THESE EXIST. Deleting the values.local.yaml files removed the only way
# anyone had to change a secret: you edited the file. Without a replacement the
# answer becomes "open the 1Password app", which is fine for a human at a
# desktop and useless for a script, an agent, or anyone over ssh — and a system
# whose secrets can only be edited by hand is one where secrets stop getting
# rotated.
#
# The shape is the same for both verbs: resolve into tmpfs, let something change
# it, push. Nothing touches a disk, and the push path is the one `push` already
# uses, so the merge-and-verify behaviour is identical.

# Resolve a chart argument to its template and logical name. Accepts a chart
# directory (infra/duckdns) with an optional stem for charts that own more than
# one secret (the deprecated web/apartment-watch was the only one: `criteria`).
# Where a template SHOULD live, with no synthesis. cmd_new is the only caller:
# it is creating the template, so a synthesised one in scratch would read as
# "already exists" and refuse — which is exactly what happened the first time.
chart_tpl_path() {
  local chart="${1:?}" stem="${2:-values.local}" pair name
  chart="${chart%/}"; chart="${chart#./}"; chart="${chart#"${ROOT:-/nonexistent}"/}"
  for pair in "${EXTERNAL_MAP[@]}"; do
    if [[ "${pair%%:*}" == "$chart" ]]; then
      name="${pair#*:}"; printf '%s\n' "${EXTERNAL_BASE}/${name}/${stem}.tpl.yaml"; return 0
    fi
  done
  printf '%s\n' "${ROOT}/${chart}/${stem}.tpl.yaml"
}

resolve_chart() {
  local chart="${1:?chart required}" stem="${2:-values.local}" tpl logical
  chart="${chart%/}"; chart="${chart#./}"; chart="${chart#"${ROOT:-/nonexistent}"/}"
  logical="${chart}/${stem}.yaml"
  tpl=""
  if [[ -n "$ROOT" ]]; then
    tpl="${ROOT}/${chart}/${stem}.tpl.yaml"
    if [[ ! -f "$tpl" ]]; then
      # External charts keep their template in the standalone clone, because
      # that is the tree their own upgrade.sh runs from.
      local pair name
      tpl=""
      for pair in "${EXTERNAL_MAP[@]}"; do
        [[ "${pair%%:*}" == "$chart" ]] || continue
        name="${pair#*:}"
        [[ -f "${EXTERNAL_BASE}/${name}/${stem}.tpl.yaml" ]] && tpl="${EXTERNAL_BASE}/${name}/${stem}.tpl.yaml"
        break
      done
    fi
  fi

  # NO TEMPLATE ON DISK IS NOT A DEAD END. The template is only a stored copy of
  # a derivation anyone can redo: item title is the chart path with / -> -, and
  # the field label is the file name. Synthesising it is what lets show and edit
  # work with no checkout at all — which is the entire point of installing this
  # as a single binary on PATH.
  #
  # The real template still wins when there is one: it is authoritative, and a
  # chart whose reference was ever hand-edited would not survive a re-derivation.
  if [[ -z "$tpl" || ! -f "$tpl" ]]; then
    local syn; syn="$(scratch)/${stem}.tpl.yaml"
    printf '{{ op://%s/%s/%s }}\n' "$VAULT" "$(item_title "$logical")" "$(field_label "$logical")" > "$syn"
    tpl="$syn"
  fi
  printf '%s\t%s\n' "$tpl" "$logical"
}

# Open $EDITOR on a tmpfs file, with the editor's own scratch writing disabled.
#
# THE EDITOR IS THE LEAK HERE, NOT THE FILE. vim writes ~/.viminfo, which holds
# registers and the last search — and you will yank a token. With undofile and a
# global undodir it writes a full undo history of the secret onto ext4. VS Code
# keeps local history under ~/.config/Code. Putting the file on tmpfs achieves
# nothing if the editor copies it somewhere else.
edit_in_place() {
  local f="$1" ed; ed="${VISUAL:-${EDITOR:-vi}}"
  case "$(basename "$ed")" in
    vim|nvim|vi)
      "$ed" -n -i NONE --cmd 'set noundofile nobackup nowritebackup noswapfile' "$f" ;;
    nano)
      "$ed" --nonewlines "$f" ;;
    *)
      note "WARNING: ${ed} may write backups or history outside tmpfs; vim/nvim/nano are hardened, others are not"
      "$ed" "$f" ;;
  esac
}

# Read the replacement document from stdin. The affordance that makes these
# verbs usable by anything that is not a human at a terminal.
read_stdin_to() {
  local f="$1"
  [[ ! -t 0 ]] || die "--from-stdin given but stdin is a terminal"
  ( umask 077; cat > "$f" )
  [[ -s "$f" ]] || die "--from-stdin read nothing"
}

# Assert the buffer is YAML before it is allowed near the vault. op inject is a
# raw text substitution and will happily store anything.
check_yaml() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$1" 2>/dev/null \
    || die "not valid YAML — refusing to store it"
}

cmd_edit() {
  need_op
  [[ $# -gt 0 ]] || die "usage: secrets.sh edit <chart> [stem]   (e.g. infra/duckdns, or '<chart> criteria' for a chart owning a second secret file)"
  local chart="$1" stem="${2:-values.local}" tpl logical sd f before after
  IFS=$'\t' read -r tpl logical < <(resolve_chart "$chart" "$stem")
  [[ -f "$tpl" ]] || die "no template for ${chart} (${stem}.tpl.yaml). New secret? use: secrets.sh new ${chart}"

  sd="$(scratch)"; f="$sd/${stem}.yaml"
  ( umask 077; : > "$f" )
  inject_probe "$tpl" "$f" || die "could not resolve ${logical} from the vault — run: scripts/secrets.sh check ${chart}"
  before="$(sha256sum "$f" | cut -d' ' -f1)"

  if [[ $FROM_STDIN -eq 1 ]]; then read_stdin_to "$f"; else edit_in_place "$f"; fi

  after="$(sha256sum "$f" | cut -d' ' -f1)"
  if [[ "$before" == "$after" ]]; then note "no change"; return 0; fi
  check_yaml "$f"

  PUSH_FROM="$f" cmd_push "${ROOT}/${logical}"
}

cmd_new() {
  need_op
  need_repo new "writes a values.local.tpl.yaml into a git repo"
  [[ $# -gt 0 ]] || die "usage: secrets.sh new <chart> [stem]"
  local chart="$1" stem="${2:-values.local}" tpl logical sd f title dir example
  chart="${chart%/}"; chart="${chart#./}"; chart="${chart#"$ROOT"/}"
  logical="${chart}/${stem}.yaml"
  tpl="$(chart_tpl_path "$chart" "$stem")"
  dir="$(dirname "$tpl")"
  [[ -d "$dir" ]] || die "no such chart directory: ${dir}"
  [[ -f "$tpl" ]] && die "${tpl#"$ROOT"/} already exists — use: $(basename "$0") edit ${chart}"

  title="${ITEM_OVERRIDE:-$(item_title "$logical")}"
  op item get "$title" --vault "$VAULT" >/dev/null 2>&1 \
    && die "vault item ${title} already exists but ${stem}.tpl.yaml does not.
       Write the template by hand, or pass --item to name a different item."

  sd="$(scratch)"; f="$sd/${stem}.yaml"
  example="${dir}/${stem}.yaml.example"
  [[ "$stem" == "values.local" ]] || example="${dir}/${stem}.example.yaml"
  if [[ -f "$example" ]]; then
    ( umask 077; cp "$example" "$f" )
    note "seeded from $(basename "$example") — it is the schema and the provenance note"
  else
    ( umask 077; printf '# %s\n' "$logical" > "$f" )
    note "no ${stem}.yaml.example — starting empty (consider writing one; it is the only human-readable record of shape)"
  fi

  if [[ $FROM_STDIN -eq 1 ]]; then read_stdin_to "$f"; else edit_in_place "$f"; fi
  check_yaml "$f"
  grep -qE '(REPLACE_ME|CHANGE-ME|000000000000)' "$f" \
    && die "placeholder values are still in the document — refusing to store it"

  note "creating op://${VAULT}/${title}/$(field_label "$logical")"
  # import, not push: push only EDITS an item that already exists, and it also
  # writes and stages the template — which is the artifact that makes the secret
  # visible to every other machine.
  #
  # WHICH TREE GETS THE TEMPLATE. For an app-repo chart, $dir is the standalone
  # clone, because that is the tree its own upgrade.sh runs from and the tree
  # where a commit can actually be made. Handing import the repo-relative path
  # would target the SUBMODULE checkout instead — which submodules-lock.sh keeps
  # at 0555, so the write fails AFTER the vault item has been created, leaving an
  # item with no template. That orphan state is exactly what made
  # web/diemer-codes invisible to check, status and every backup.
  #
  # import's out-of-repo branch handles this: an absolute path outside $ROOT
  # writes the template beside it, and --item keeps the vault name matching the
  # submodule path so a deploy from either tree resolves the same reference.
  if [[ "$dir" == "$ROOT"/* ]]; then
    ITEM_OVERRIDE="$title" PUSH_FROM="$f" cmd_import "$logical"
  else
    ITEM_OVERRIDE="$title" PUSH_FROM="$f" cmd_import "${dir}/$(field_label "$logical")"
  fi
}

# Keys only unless asked otherwise. A terminal is not a safe sink for a secret:
# in the claude-workspace pod it is a tmux session on a PVC, so a revealed value
# stays in the scrollback long after the command scrolls away. This is also the
# verb an agent should reach for to find out what a chart holds.
cmd_show() {
  need_op
  [[ $# -gt 0 ]] || die "usage: secrets.sh show <chart> [stem] [--reveal]"
  local chart="$1" stem="${2:-values.local}" tpl logical sd f
  IFS=$'\t' read -r tpl logical < <(resolve_chart "$chart" "$stem")
  [[ -f "$tpl" ]] || die "no template for ${chart} (${stem}.tpl.yaml)"
  sd="$(scratch)"; f="$sd/${stem}.yaml"
  ( umask 077; : > "$f" )
  inject_probe "$tpl" "$f" || die "could not resolve ${logical} from the vault"
  if [[ $REVEAL -eq 1 ]]; then cat "$f"; return 0; fi
  python3 - "$f" <<'PY'
import sys, yaml
def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        print(f"  {path:<44} [{len(node)} item(s)]")
    else:
        s = str(node)
        kind = "empty" if s == "" else f"{len(s)} chars"
        print(f"  {path:<44} <{kind}>")
try:
    walk(yaml.safe_load(open(sys.argv[1])) or {})
except Exception as e:
    sys.exit(f"could not parse: {e}")
PY
  note "values elided — pass --reveal to print them (a terminal is not a safe sink)"
}

cmd_import() {
  need_op
  [[ $# -gt 0 ]] || die "usage: secrets.sh import <path-to-values.local.yaml> ..."
  local f title label tpl src tplabs
  for f in "$@"; do
    # Accept both repo-relative paths and absolute paths outside the repo. The
    # standalone app clones (~/Code/gamedex etc.) are deployed from, and own
    # their own chart, so their template has to be written there — but the item
    # must be named for the SUBMODULE path, because the same committed template
    # is what a deploy from selfhosted/games/gamedex reads. Hence --item.
    if [[ "$f" = /* && "$f" != "$ROOT"/* ]]; then
      # --from-file applies here too: `new` for an app-repo chart resolves the
      # buffer in tmpfs and names the destination in the clone, which does not
      # exist yet by definition.
      src="${PUSH_FROM:-$f}"
      [[ -f "$src" ]] || die "no such file: $src"
      [[ -n "$ITEM_OVERRIDE" ]] \
        || die "$src is outside the repo — pass --item <title> so it matches the submodule path"
      # Derived from $f, the NAMED DESTINATION, never from $src. With --from-file
      # those differ: src is a tmpfs buffer, and deriving the template path from
      # it writes the template into tmpfs, where it evaporates — leaving a vault
      # item with no template, which is the invisible-secret state this whole
      # change exists to prevent.
      title="$ITEM_OVERRIDE"; label="$(field_label "$f")"
      tplabs="$(dirname "$f")/$([[ "$label" == values.local.yaml ]] && echo values.local.tpl.yaml || echo "${label%.yaml}.tpl.yaml")"
    else
      f="${f#"$ROOT"/}"; f="${f#./}"
      # --from-file: the bytes may live in tmpfs (see cmd_new). The path
      # argument still supplies the logical identity and the template location.
      [[ -n "$PUSH_FROM" || -f "${ROOT}/${f}" ]] || die "no such file: $f"
      is_excluded "$f" && die "$f is deliberately excluded (see EXCLUDE_FILES)"
      src="${PUSH_FROM:-${ROOT}/${f}}"
      title="${ITEM_OVERRIDE:-$(item_title "$f")}"; label="$(field_label "$f")"
      tpl="$(dirname "$f")/$([[ "$label" == values.local.yaml ]] && echo values.local.tpl.yaml || echo "${label%.yaml}.tpl.yaml")"
      tplabs="${ROOT}/${tpl}"
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
      echo "    would create op://${VAULT}/${title}/${label} and write ${tplabs#"$ROOT"/}"; continue
    fi

    # Never pass the secret as an argv value: argv is visible in /proc and lands
    # in shell history. Strip exactly one trailing newline — $(cat) would strip
    # all of them and break the byte-equality gate below.
    #
    # Both verbs take a JSON body the same way, and neither accepts the secret as
    # an argv assignment:
    #   create  --template FILE
    #   edit    --template FILE
    #
    # EDIT USED TO PASS '-' AND SILENTLY DID NOTHING. op's grammar is
    # `op item edit <item> [<assignment>...]`, so a bare '-' is taken as an
    # assignment rather than as "the JSON is on stdin"; op then never reads
    # stdin, changes nothing, and EXITS 0 — so `|| die` never fired and push
    # reported success on every no-op. It hid for as long as it did because the
    # migration only ever CREATED items, and create already used --template.
    # (op's own help pipes with no '-' at all: `cat x.json | op item edit item`.
    # --template is that same path without depending on argv-vs-stdin
    # precedence, and it is what the help documents first.)
    local sd body_json existing; sd="$(scratch)"
    body_json="$sd/item.json"; existing="$sd/existing.json"

    # `op item edit` REPLACES the field set with what it is given; it does not
    # merge. web/apartment-watch owns two files (values.local.yaml and
    # criteria.yaml) on one item, so writing the second would silently drop the
    # first. Fetch the current fields and merge into them.
    if op item get "$title" --vault "$VAULT" --format json > "$existing" 2>/dev/null; then
      chmod 600 "$existing"
      note "updating item ${title} (field ${label})"
    else
      : > "$existing"
      note "creating item ${title} (field ${label})"
    fi
    chmod 600 "$existing"

    python3 - "$src" "$title" "$label" "$existing" > "$body_json" <<'PY'
import json, sys, pathlib
body = pathlib.Path(sys.argv[1]).read_text()
if body.endswith("\n"): body = body[:-1]   # exactly one; the template supplies it
title, label, existing_path = sys.argv[2], sys.argv[3], sys.argv[4]

fields, raw = [], pathlib.Path(existing_path).read_text().strip()
if raw:
    for fld in (json.loads(raw).get("fields") or []):
        if fld.get("label") == label or fld.get("id") == label:
            continue                       # replaced below
        keep = {k: fld[k] for k in ("id", "label", "type", "value") if k in fld}
        # Carry forward only fields that HOLD something. An empty built-in
        # (notesPlain is always present on a SECURE_NOTE, usually blank) would be
        # sent back and then re-added by op on top of itself, so the item grows a
        # duplicate notesPlain on every single edit.
        if keep.get("value"):
            fields.append(keep)
fields.append({"id": label, "label": label, "type": "STRING", "value": body})
print(json.dumps({"title": title, "category": "SECURE_NOTE", "fields": fields}))
PY
    chmod 600 "$body_json"

    # </dev/null IS LOAD-BEARING, on both verbs. op refuses --template when it
    # also sees data on stdin ("cannot edit an item from template and stdin at
    # the same time"), and it decides that by whether stdin is a terminal — not
    # by whether anything was actually written. Every caller here runs inside
    # `done < <(discover_pairs)`, so stdin is a pipe; under systemd there is no
    # tty either. Both conditions are invisible until a push has something to
    # push, which is why this survived: the timer reported "0 pushed" for days
    # and then failed the first time a file actually differed.
    if [[ -s "$existing" ]]; then
      op item edit "$title" --vault "$VAULT" --template "$body_json" >/dev/null </dev/null \
        || die "op item edit failed for ${title}"
    else
      op item create --vault "$VAULT" --template "$body_json" >/dev/null </dev/null \
        || die "op item create failed for ${title}"
    fi

    # A whole-file template is EXACTLY one line and carries no comments.
    # op inject is a raw text substitution, so anything else in the template —
    # including a helpful header — is copied verbatim into values.local.yaml.
    # That breaks byte-equality with the stored blob, and a later `push` would
    # write the comments back into the vault, compounding on every round-trip.
    # The one-liner is documented in the README and in values.local.yaml.example
    # instead. Per-key templates (Phase 2) are hand-authored and may comment
    # freely — they have no stored blob to match.
    #
    # The template's trailing newline supplies the file's, because the stored
    # blob has exactly one stripped. That only round-trips if the source file
    # HAD a trailing newline — web/kelsey-green/values.local.yaml does not, and
    # unconditionally adding one made it 687 bytes against the original's 686.
    # So mirror the source: `tail -c1` yields the last byte, and command
    # substitution strips a newline, so an empty result means "ends with \n".
    if [[ -z "$(tail -c1 "$src")" ]]; then
      printf '{{ op://%s/%s/%s }}\n' "$VAULT" "$title" "$label" > "$tplabs"
    else
      printf '{{ op://%s/%s/%s }}'   "$VAULT" "$title" "$label" > "$tplabs"
    fi
    note "wrote ${tplabs#"$ROOT"/}"

    # Leave no broken template behind: a tpl in the tree that does not reproduce
    # its file is worse than no tpl, because upgrade.sh would materialize from it.
    if ! verify_one "$tplabs" "$src"; then
      rm -f "$tplabs"
      die "$src does not round-trip — removed ${tplabs}; vault item ${title} left for inspection"
    fi
    # Round-tripped, so both sides demonstrably agree: settle the marker now
    # rather than leaving the first sync to report drift it cannot arbitrate.
    marker_write_tl "$title" "$label" "$src"
  done
}

# Gate 1: byte equality, on absolute paths — one implementation for the charts
# here and for the standalone app clones. (This used to be two near-identical
# functions, verify_one and verify_abs, differing only in whether they prepended
# $ROOT; making every path absolute collapsed them.) Gate 2 is cmd_verify.
# GATE 1: can the vault produce this secret, and — if a local file still exists —
# does it reproduce that file byte for byte?
#
# THE MISSING-FILE CASE IS SUCCESS, NOT FAILURE. This used to `cmp` against $out
# with no existence check, so a chart with no local file reported
# "GATE1 FAIL (differs)" and GATE2 told you not to commit a template that was
# perfectly correct. That was harmless while every secret was also a file. It
# stopped being harmless the moment the files were deleted: not-materialized is
# now the NORMAL state for all thirty charts, so verify failed on a completely
# healthy fleet — and a check that cries wolf every time is one you stop reading,
# which costs more than not having it.
verify_one() {
  local tpl="$1" out="$2" tmp rc=0
  tmp="$(scratch)"
  if ! inject_probe "$tpl" "$tmp/probe"; then rm -rf "$tmp"; echo "    GATE1 FAIL (unresolvable)"; return 1; fi
  if [[ ! -f "$out" ]]; then
    echo "    GATE1 resolves (nothing on disk to compare — expected)"
  elif cmp -s "$out" "$tmp/probe"; then
    echo "    GATE1 byte-exact"
  else
    echo "    GATE1 FAIL (differs from the on-disk file)"; rc=1
  fi
  rm -rf "$tmp"; return $rc
}

# Map an absolute path inside a standalone clone back to its logical identity —
# the submodule path the vault item is named for. Empty if it is not one.
logical_for_external() {
  local abs="$1" pair logical dir
  for pair in "${EXTERNAL_MAP[@]}"; do
    logical="${pair%%:*}"; dir="${EXTERNAL_BASE}/${pair#*:}"
    [[ "$abs" == "$dir"/* ]] && { printf '%s/%s\n' "$logical" "${abs#"$dir"/}"; return 0; }
  done
  return 1
}

# The other direction, for error messages: submodule path -> the clone to use.
external_clone_hint() {
  local logical="$1" pair sub
  for pair in "${EXTERNAL_MAP[@]}"; do
    sub="${pair%%:*}"
    [[ "$logical" == "$sub"/* ]] && { printf '%s/%s/%s\n' "$EXTERNAL_BASE" "${pair#*:}" "${logical#"$sub"/}"; return 0; }
  done
  printf '%s\n' "(no standalone clone is mapped for ${logical})"
}

cmd_push() {
  need_op
  [[ $# -gt 0 ]] || die "usage: secrets.sh push <path> ..."
  local f src logical title label
  for f in "$@"; do
    # Three accepted shapes, resolved to one logical identity: repo-relative,
    # absolute inside the repo, and absolute inside a standalone clone. The last
    # one is why external edits had no way into the vault before — push simply
    # could not name them.
    [[ "$f" == /* ]] || f="$(cd "$(dirname "$f")" 2>/dev/null && pwd)/$(basename "$f")" || true
    if [[ "$f" == "$ROOT"/* ]]; then
      logical="${f#"$ROOT"/}"; src="$f"
    elif logical="$(logical_for_external "$f")"; then
      src="$f"
    else
      die "no such managed file: $f"
    fi
    # --from-file separates WHERE THE BYTES ARE from WHAT THEY ARE CALLED. `edit`
    # needs exactly that: the content it is pushing lives in tmpfs, which is not
    # in the repo and not in any clone, so it could never be named. The path
    # argument still supplies the logical identity; only the source moves.
    [[ -n "$PUSH_FROM" ]] && src="$PUSH_FROM"
    [[ -f "$src" ]] || die "no such file: $src"
    is_mirror "$logical" && [[ "$src" == "$ROOT"/* ]] \
      && die "$logical is a submodule checkout, not a source — push the standalone clone instead:
       $(external_clone_hint "$logical")"
    title="$(item_title "$logical")"; label="$(field_label "$logical")"
    if [[ $DRY_RUN -eq 1 ]]; then echo "    would push $logical -> op://${VAULT}/${title}/${label}"; continue; fi
    note "pushing ${logical} -> op://${VAULT}/${title}/${label}"
    local sd bj existing; sd="$(scratch)"
    bj="$sd/item.json"; existing="$sd/existing.json"

    # The same two rules import obeys, and for the same reasons: --template
    # rather than '-' (see the note in cmd_import — '-' is read as an assignment
    # and the edit silently no-ops), and MERGE into the item's current fields,
    # because edit replaces the field set wholesale. Pushing a single field
    # without merging would have quietly deleted apartment-watch's criteria.yaml
    # the first time anyone pushed its values.local.yaml.
    op item get "$title" --vault "$VAULT" --format json > "$existing" 2>/dev/null \
      || die "no vault item ${title} — run: secrets.sh import ${f}"
    chmod 600 "$existing"

    python3 - "$src" "$title" "$label" "$existing" > "$bj" <<'PY'
import json, sys, pathlib
body = pathlib.Path(sys.argv[1]).read_text()
if body.endswith("\n"): body = body[:-1]   # exactly one; the template supplies it
title, label, existing_path = sys.argv[2], sys.argv[3], sys.argv[4]

fields, raw = [], pathlib.Path(existing_path).read_text().strip()
if raw:
    for fld in (json.loads(raw).get("fields") or []):
        if fld.get("label") == label or fld.get("id") == label:
            continue                       # replaced below
        keep = {k: fld[k] for k in ("id", "label", "type", "value") if k in fld}
        # Carry forward only fields that HOLD something. An empty built-in
        # (notesPlain is always present on a SECURE_NOTE, usually blank) would be
        # sent back and then re-added by op on top of itself, so the item grows a
        # duplicate notesPlain on every single edit.
        if keep.get("value"):
            fields.append(keep)
fields.append({"id": label, "label": label, "type": "STRING", "value": body})
print(json.dumps({"title": title, "category": "SECURE_NOTE", "fields": fields}))
PY
    chmod 600 "$bj"
    # </dev/null: see the note on the same call in cmd_import. This is the site
    # that actually broke — 2026-08-13, "cannot edit an item from template and
    # stdin at the same time", every 15 minutes once a file finally differed.
    op item edit "$title" --vault "$VAULT" --template "$bj" >/dev/null </dev/null \
      || die "push failed for ${f}"

    # PROVE IT LANDED. The whole reason this bug survived is that op reported
    # success while changing nothing, so a push that cannot show the vault now
    # reproduces the file is a failed push, not a finished one.
    local tpl_abs; tpl_abs="$(dirname "$src")/$([[ "$label" == values.local.yaml ]] \
      && echo values.local.tpl.yaml || echo "${label%.yaml}.tpl.yaml")"
    if [[ -f "$tpl_abs" ]]; then
      verify_one "$tpl_abs" "$src" >/dev/null \
        || die "pushed ${logical} but the vault does not reproduce it — nothing is backed up"
    fi
    # Proven equal, so this is the new agreed point for conflict detection.
    marker_write_tl "$title" "$label" "$src"
  done
  # A backup can never be more than one change stale. cmd_sync suppresses this
  # and takes one backup at the end instead of one per file.
  #
  # auto_backup subshells cmd_backup so the intended `|| true` actually holds:
  # cmd_backup ends in `die`, and die exits the script, so an unreachable NAS
  # used to discard the exit status of a push that had already succeeded. It
  # also skips the archive entirely where one cannot be taken (the pod).
  [[ $DRY_RUN -eq 1 || $NO_BACKUP -eq 1 ]] || auto_backup || true
}

# Gate 2 — render equality. The check that actually matters: an empty diff means
# the cluster would receive byte-identical manifests, proved entirely offline.
# No helm upgrade is needed to validate this migration.
cmd_verify() {
  need_op
  need_repo verify "renders every chart with helm"
  local tmp; tmp="$(scratch)"
  local logical tpl out dir rc=0
  while IFS=$'\t' read -r logical tpl out; do
    [[ -n "$logical" ]] || continue
    if [[ $# -gt 0 ]] && ! printf '%s\n' "$@" | grep -qF "$(dirname "$logical")"; then continue; fi
    dir="$(dirname "$out")"          # where the chart actually is, repo or clone
    echo "  $(dirname "$logical")"
    verify_one "$tpl" "$out" || rc=1
    if [[ ! -f "${dir}/Chart.yaml" ]]; then
      echo "    GATE2 skipped (values-only project — render against the upstream chart by hand)"
      continue
    fi
    if gate2_skipped "$(dirname "$logical")"; then
      echo "    GATE2 skipped (render needs inputs this gate does not supply — see GATE2_SKIP)"
      continue
    fi
    inject_probe "$tpl" "$tmp/probe" || { rc=1; continue; }
    if [[ -f "$out" ]]; then
      # Migration gate: the vault and the file must render the same manifests.
      if diff -q \
          <(helm template gate "$dir" -f "${dir}/values.yaml" -f "$out" 2>/dev/null) \
          <(helm template gate "$dir" -f "${dir}/values.yaml" -f "$tmp/probe" 2>/dev/null) >/dev/null; then
        echo "    GATE2 render-identical"
      else
        echo "    GATE2 FAIL — rendered manifests differ. Do NOT commit this template."
        rc=1
      fi
    else
      # Steady state: there is no file to compare against, so the question is
      # simply whether what the vault produces renders at all. A chart that
      # renders EMPTY is the failure worth catching — that is what a `required`
      # gate silently passing, or a vault field gone blank, looks like from here.
      if [[ -s "$tmp/render" ]] || helm template gate "$dir" -f "${dir}/values.yaml" -f "$tmp/probe" > "$tmp/render" 2>/dev/null; then
        if [[ -s "$tmp/render" ]]; then
          echo "    GATE2 renders from the vault ($(wc -l < "$tmp/render") lines)"
        else
          echo "    GATE2 FAIL — renders empty from the vault"; rc=1
        fi
      else
        echo "    GATE2 FAIL — does not render from the vault"; rc=1
      fi
      rm -f "$tmp/render"
    fi
    rm -f "$tmp/probe"
  done < <(discover_pairs)
  return $rc
}

# ------------------------------------------------------------------- sync
#
# The verb that removes the churn. Everything else here is a manual instrument;
# this is the one a timer runs, so it is deliberately conservative: it acts only
# where the marker proves which side moved, and it stops rather than guessing.
cmd_sync() {
  need_op
  local tmp; tmp="$(scratch)"
  local logical tpl out verdict
  local pushed=0 pulled=0 conflicts=0 unknown=0 unresolvable=0 fresh=0

  # cmd_push takes a NAS backup per file ("a backup can never be more than one
  # change stale"). Reconciling twelve files would take twelve backups, so
  # suppress it here and take exactly one at the end.
  NO_BACKUP=1

  local refreshed=0
  while IFS=$'\t' read -r logical tpl out; do
    [[ -n "$logical" ]] || continue
    verdict="$(classify_one "$logical" "$tpl" "$out" "$tmp/probe")"

    # A session that died part-way through a run is indistinguishable from a
    # broken template: every probe from that point on fails, and op's message
    # for both is "You are not currently signed in". So on the first such
    # failure, force a re-validation (which signs in again if the session is
    # genuinely gone) and give that one file another go.
    #
    # Deliberately here and not inside classify_one: that runs in a command
    # substitution, so a sign-in there would be discarded with the subshell and
    # repeated for every remaining file — each one invalidating the last.
    if [[ "$verdict" == "UNRESOLVABLE" && $refreshed -eq 0 ]]; then
      refreshed=1
      OP_SESSION_READY=0
      if op_session_ready; then
        note "session was stale — signed in again and retrying ${logical}"
        verdict="$(classify_one "$logical" "$tpl" "$out" "$tmp/probe")"
      fi
    fi

    case "$verdict" in
      in-sync)
        # Cheap and worth doing: an agreeing pair with no marker is exactly the
        # state every chart is in right after the migration, and recording it
        # now is what lets the NEXT divergence be arbitrated instead of flagged.
        [[ -n "$(marker_read "$logical")" ]] || { marker_write "$logical" "$out"; fresh=$((fresh+1)); }
        ;;
      not-materialized)
        if [[ $DRY_RUN -eq 1 ]]; then echo "  would materialize  $logical"
        else
          inject_to "$tpl" "$out" && { marker_write "$logical" "$out"; echo "  materialized  $logical"; }
        fi
        pulled=$((pulled+1))
        ;;
      pull)
        if [[ $DRY_RUN -eq 1 ]]; then echo "  would pull    $logical"
        else
          inject_to "$tpl" "$out" && { marker_write "$logical" "$out"; echo "  pulled        $logical"; }
        fi
        pulled=$((pulled+1))
        ;;
      push)
        if [[ $DRY_RUN -eq 1 ]]; then echo "  would push    $logical"
        else
          cmd_push "$out" >/dev/null && echo "  pushed        $logical"
        fi
        pushed=$((pushed+1))
        ;;
      conflict)
        echo "  CONFLICT      $logical — both sides changed since the last sync"
        echo "                keep the vault: secrets.sh pull --force $(dirname "$logical")"
        echo "                keep the file:  secrets.sh push ${out}"
        conflicts=$((conflicts+1))
        ;;
      unknown)
        echo "  DRIFT         $logical — no sync marker, so which side moved is unknowable"
        echo "                settle it once with push or pull; sync handles it from then on"
        unknown=$((unknown+1))
        ;;
      UNRESOLVABLE)
        # Counted apart from conflicts: a conflict is a decision waiting for a
        # human, this is a broken template or an unreachable vault. Both stop
        # the run, but calling them the same thing makes the summary lie.
        echo "  UNRESOLVABLE  $logical — its template does not resolve against the vault"
        unresolvable=$((unresolvable+1))
        ;;
    esac
    rm -f "$tmp/probe"
  done < <(discover_pairs)

  NO_BACKUP=0
  [[ $fresh -gt 0 ]] && note "recorded ${fresh} baseline marker(s)"
  note "${pushed} pushed, ${pulled} pulled, ${conflicts} conflict(s), ${unknown} unarbitrated, ${unresolvable} unresolvable"

  if [[ $DRY_RUN -eq 0 && $(( pushed + pulled )) -gt 0 ]]; then
    # SUBSHELLS, not plain `|| echo`. Both of these end in `die` on failure, and
    # die runs `exit` — which unwinds this whole script rather than the function,
    # so the `||` would never fire and a NAS that happens to be down would take
    # the cluster publish down with it. The reconciliation already succeeded by
    # this point; neither of these is allowed to retroactively fail it.
    auto_backup || echo "    backup failed — the vault still holds the change"
  fi

  # Non-zero on anything a human has to look at. A timer that always exits 0
  # cannot tell you it has stopped being useful.
  [[ $(( conflicts + unknown + unresolvable )) -eq 0 ]]
}

# ------------------------------------------------------- cluster relay

# MATERIALIZES FROM THE VAULT, rather than copying whatever happens to be lying
# around. It used to `cp` each discovered file off the disk, which was fine while
# every secret was also a file — and became a silent hole the moment they were
# not: with the files gone, discover_local yields nothing, the files/ half of the
# archive is empty, and the tar still builds and still uploads and still passes
# its read-back check. A backup that quietly drops half its content is the one
# failure mode a backup must not have; this script's own header says so.
#
# Driven off discover_pairs (templates), not discover_local (files), so what
# lands in the archive is what the VAULT can produce — which is exactly what a
# restore would give you back.
pack_bundle() {
  local dest="$1" tmp logical tpl out name missing=0
  tmp="$(scratch)"
  mkdir -p "$tmp/files"
  while IFS=$'\t' read -r logical tpl out; do
    [[ -n "$logical" ]] || continue
    mkdir -p "$tmp/files/$(dirname "$logical")"
    if inject_probe "$tpl" "$tmp/files/$logical"; then
      chmod 600 "$tmp/files/$logical"
    else
      note "WARNING: ${logical} could not be resolved — omitted from the archive"
      missing=$((missing+1))
    fi
  done < <(discover_pairs)
  [[ $missing -eq 0 ]] || die "${missing} secret(s) unresolvable; refusing to write a partial archive"
  # Exclude MANIFEST from its own listing: the shell creates it at redirect time,
  # so find would catch it mid-write and record a hash that never matches. A
  # wrong hash in the integrity record is worse than no entry.
  (cd "$tmp" && find . -type f ! -name MANIFEST | sed 's|^\./||' | sort | while read -r p; do
     printf '%s\t%s\t%s\n' "$(stat -c%s "$p")" "$(sha256sum "$p" | cut -d' ' -f1)" "$p"
   done) > "$tmp/MANIFEST"
  tar czf "$dest" -C "$tmp" .
  rm -rf "$tmp"
}


# ------------------------------------------------------------ NAS backup

ensure_age_key() {
  mkdir -p "$(dirname "$AGE_KEY")"; chmod 700 "$(dirname "$AGE_KEY")"
  if [[ ! -f "$AGE_KEY" ]]; then
    note "generating a new age identity at ${AGE_KEY}"
    (umask 077; age-keygen -o "$AGE_KEY" 2>/dev/null) || die "age-keygen failed"
  fi
  local pub; pub="$(age-keygen -y "$AGE_KEY")"
  if [[ ! -f "$AGE_RECIPIENTS" ]] || ! grep -qF "$pub" "$AGE_RECIPIENTS"; then
    printf '# age public keys that backups are encrypted to. Public — safe to commit.\n# Any checkout can CREATE a backup; only the paper key can READ one.\n%s\n' \
      "$pub" >> "$AGE_RECIPIENTS"
    note "added this machine's public key to scripts/backup-recipients.txt (commit it)"
  fi

  # A backup encrypted to a key that exists only on this laptop is not a backup.
  # It fails silently, and you find out at exactly the wrong moment.
  if [[ ! -f "$PAPER_MARKER" ]]; then
    local priv tail6
    priv="$(grep -v '^#' "$AGE_KEY" | grep AGE-SECRET-KEY | head -1)"
    tail6="${priv: -6}"
    cat >&2 <<EOF

  ────────────────────────────────────────────────────────────────────────
  The age private key exists only on this machine.

      ${priv}

  Write it on paper NOW. On the SAME sheet write your 1Password Secret Key
  and account password — 'op account add' on a replacement machine needs
  them, and without them the 1Password half has the identical hole.

  This key is one line of base32 with no ambiguous glyphs: it is meant to be
  transcribed by hand. Store it with your vital documents.
  ────────────────────────────────────────────────────────────────────────

EOF
    [[ $ASSUME_YES -eq 1 ]] && die "refusing to back up unattended before the paper key is confirmed"
    read -r -p "  Type the last 6 characters of the key to continue: " ans
    [[ "$ans" == "$tail6" ]] || die "did not match — nothing was backed up"
    touch "$PAPER_MARKER"
    note "acknowledged (${PAPER_MARKER})"
  fi
}

# Can a NAS archive be taken from HERE? The claude-workspace pod cannot: its
# image ships neither age nor smbclient, its NetworkPolicy has no route to the
# LAN (RFC1918 is excluded from the egress rule), and it holds no NAS
# credentials. That is by design — the archive is the laptop's job — but push
# chains a backup onto every write, so a pod push ended in a bare
# "FAIL: age required" printed *after* the vault had already been updated. The
# write succeeded and the message said the opposite.
#
# Only the automatic after-a-write backup degrades to a note. Asking for
# `backup` or `restore` by name still dies loudly: there the archive IS the
# request, and quietly not making one is the silent hole this file keeps
# warning about.
backup_possible() {
  command -v age >/dev/null && command -v smbclient >/dev/null && [[ -f "$NAS_CREDS" ]]
}

auto_backup() {
  backup_possible || {
    note "no NAS backup from here (needs age, smbclient and ${NAS_CREDS}) — the vault holds the change; the laptop's next backup picks it up"
    return 0
  }
  ( cmd_backup )
}

cmd_backup() {
  command -v age >/dev/null || die "age required"
  ensure_age_key
  local tmp stamp archive; tmp="$(scratch)"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"; archive="selfhosted-secrets-${stamp}.tar.gz.age"

  mkdir -p "$tmp/stage"
  INCLUDE_EXTERNAL=1 pack_bundle "$tmp/stage/files.tar.gz"

  # A full vault dump, so 1Password itself can be rebuilt — not just the files.
  # --reveal is required or concealed field values come back masked.
  #
  # This used to swallow every error (2>/dev/null … || true) and then report
  # whatever jq found, which was 0. A session that expires between `op whoami`
  # and `op item list` produced an archive with an EMPTY items.json and no
  # indication anything was wrong — a silent hole in the half of the backup that
  # exists specifically for losing 1Password. Count the items and insist they
  # match, or say plainly that this is a files-only archive.
  local want=0 got=0
  if [[ $FILES_ONLY -eq 1 ]]; then
    printf 'Files-only by request (--files-only). Every secret restores from files/;\nthe vault structure does not.\n' \
      > "$tmp/stage/VAULT-DUMP-SKIPPED"
    note "files-only backup (requested)"
  # ASK for a session; do not merely test for one. This branch used to check
  # `op whoami` and quietly degrade, which was correct when an unattended run
  # could never sign in — but now it can, and a passive check means the timer
  # keeps producing files-only archives while looking like it succeeded. That is
  # the exact silent-hole failure the paragraph above is about.
  #
  # Still degrades rather than dying if the session cannot be had: half a backup
  # beats none, and it says so on stdout and inside the archive.
  elif op_session_ready \
       && want="$(op item list --vault "$VAULT" --format json 2>/dev/null | jq 'length' 2>/dev/null)" \
       && [[ "${want:-0}" -gt 0 ]]; then
    mkdir -p "$tmp/stage/vault"
    op item list --vault "$VAULT" --format json | jq -r '.[].id' \
      | while read -r id; do op item get "$id" --vault "$VAULT" --format json --reveal; done \
      | jq -s '.' > "$tmp/stage/vault/items.json"
    got="$(jq 'length' "$tmp/stage/vault/items.json" 2>/dev/null || echo 0)"
    if [[ "$got" -ne "$want" ]]; then
      die "vault dump incomplete: got ${got} of ${want} items. Re-run 'eval \$(op signin)' first, or pass --files-only to accept a files-only archive."
    fi
    note "vault dump: ${got} item(s)"
  else
    printf 'op was unavailable or not signed in. Every secret still restores from\nfiles/; only the vault structure is missing. Re-run after `eval $(op signin)`\nfor a full archive.\n' \
      > "$tmp/stage/VAULT-DUMP-SKIPPED"
    note "op unavailable or signed out — files-only backup (secrets still restore; vault structure does not)"
  fi

  # Never write a plaintext archive to disk; /dev/shm is tmpfs, / is not.
  tar czf - -C "$tmp/stage" . | age -R "$AGE_RECIPIENTS" -o "$tmp/$archive"
  note "encrypted archive: $(stat -c%s "$tmp/$archive") bytes"

  if [[ $DRY_RUN -eq 1 ]]; then echo "    would upload ${archive} to ${NAS_SHARE}/${NAS_DIR}"; return 0; fi
  command -v smbclient >/dev/null || die "smbclient required"
  [[ -f "$NAS_CREDS" ]] || die "missing ${NAS_CREDS} (username=/password=/domain=, mode 600)"
  smbclient "$NAS_SHARE" -A "$NAS_CREDS" -c "cd ${NAS_DIR}; put ${tmp}/${archive} ${archive}" >/dev/null \
    || die "upload to ${NAS_SHARE}/${NAS_DIR} failed"
  note "uploaded ${archive}"

  # A backup you have never decrypted is not a backup. Read it back and prove it.
  smbclient "$NAS_SHARE" -A "$NAS_CREDS" -c "cd ${NAS_DIR}; get ${archive} ${tmp}/verify.age" >/dev/null \
    || die "could not read back the archive just written"
  age -d -i "$AGE_KEY" "$tmp/verify.age" | tar tzf - >/dev/null \
    || die "the backup just written cannot be decrypted"
  note "verified: readable and decryptable"

  # Retention. Names are ISO-8601 UTC, so a lexical sort is a chronological one.
  # Prune only AFTER the new archive is verified, so a failed run never costs an
  # old good copy.
  local -a archives; local old n
  mapfile -t archives < <(smbclient "$NAS_SHARE" -A "$NAS_CREDS" -c "cd ${NAS_DIR}; ls" 2>/dev/null \
    | grep -oE 'selfhosted-secrets-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.age' | sort -u)
  n=${#archives[@]}
  if [[ $n -gt $KEEP ]]; then
    for old in "${archives[@]:0:$((n - KEEP))}"; do
      smbclient "$NAS_SHARE" -A "$NAS_CREDS" -c "cd ${NAS_DIR}; del ${old}" >/dev/null 2>&1 \
        && echo "    pruned ${old}"
    done
  fi
  note "${n} archive(s) on the NAS, keeping ${KEEP}"
}

cmd_restore() {
  command -v age >/dev/null || die "age required"
  command -v smbclient >/dev/null || die "smbclient required"
  [[ -f "$AGE_KEY" ]] || die "no age key at ${AGE_KEY} — transcribe it from paper, or fetch backup-age.key.age from the NAS"
  local tmp latest; tmp="$(scratch)"
  latest="$(smbclient "$NAS_SHARE" -A "$NAS_CREDS" -c "cd ${NAS_DIR}; ls" 2>/dev/null \
            | awk '{print $1}' | grep '^selfhosted-secrets-.*\.age$' | sort | tail -1)"
  [[ -n "$latest" ]] || die "no archives found in ${NAS_SHARE}/${NAS_DIR}"
  note "latest: ${latest}"
  smbclient "$NAS_SHARE" -A "$NAS_CREDS" -c "cd ${NAS_DIR}; get ${latest} ${tmp}/a.age" >/dev/null
  age -d -i "$AGE_KEY" "$tmp/a.age" > "$tmp/a.tar.gz" || die "decryption failed"
  mkdir -p "$tmp/x" && tar xzf "$tmp/a.tar.gz" -C "$tmp/x"
  tar xzf "$tmp/x/files.tar.gz" -C "$tmp/x" 2>/dev/null || true

  if [[ $VERIFY_ONLY -eq 1 ]]; then
    note "contents (nothing written):"; sed 's/^/    /' "$tmp/x/MANIFEST" 2>/dev/null || find "$tmp/x" -type f | sed 's/^/    /'
    # The MANIFEST covers files.tar.gz only, so it says nothing about the half
    # of the archive that exists for losing 1Password itself. Report that half
    # explicitly — otherwise a files-only archive and a full one look identical
    # here, which is precisely how the weekly backup shipped without its vault
    # dump for as long as it did.
    if [[ -f "$tmp/x/vault/items.json" ]]; then
      note "vault dump: $(jq 'length' "$tmp/x/vault/items.json" 2>/dev/null || echo '?') item(s)"
    else
      note "NO VAULT DUMP in this archive — files restore, the vault structure does not"
      [[ -f "$tmp/x/VAULT-DUMP-SKIPPED" ]] && sed 's/^/    /' "$tmp/x/VAULT-DUMP-SKIPPED"
    fi
    return 0
  fi
  local f n=0
  while IFS= read -r f; do
    mkdir -p "${ROOT}/$(dirname "$f")"
    install -m600 "$tmp/x/files/$f" "${ROOT}/${f}"; echo "    $f"; n=$((n+1))
  done < <(cd "$tmp/x" && find files -type f 2>/dev/null | sed 's|^files/||' | sort)
  note "$n file(s) restored"
  [[ -f "$tmp/x/vault/items.json" ]] && note "vault dump available at ${tmp}/x/vault/items.json (copy it out before this shell exits)"
}

usage() {
  echo "Usage: $(basename "$0") <command> [options] [path...]"
  cat <<'EOF' 

  sync [--dry-run]         reconcile every managed file with the vault, both ways
  check                    op reachable, every ref resolves AND is non-empty
  status [path...]         per-chart: in-sync / drift / not-materialized / NOT MIGRATED
  pull [path...]           materialize values.local.yaml from 1Password
  verify [path...]         Gate 1 (byte-exact) + Gate 2 (rendered manifests identical)
  edit <chart> [stem]      open the vault's copy in $EDITOR (tmpfs), push on save
  new <chart> [stem]       create a secret: seed from .example, store it, write the template
  show <chart> [stem]      what a chart holds — key paths only, values elided
  import <path>...         one-time: store an existing file in the vault, write its template
  push <path>...           write local edits back to the vault, then back up
  backup                   age-encrypt everything + vault dump, upload to the NAS, verify
  restore [--verify-only]  recover from the latest NAS archive

Options: --dry-run  --force  --yes  --no-external  --vault NAME
         --from-stdin (edit/new, for scripts and agents)  --reveal (show)
         --files-only (skip the vault dump)  --keep N (archive retention, default 20)

sync is the unattended verb. It pushes when only the file moved, pulls when only
the vault moved, and refuses when both did — decided against a recorded marker,
not against mtimes. Sessions come from scripts/op-session.sh, so it runs with no
terminal attached.

The standalone app clones under ~/Code are in scope by default; --no-external
drops them. The submodule checkouts of those same repos are never a source.

The contract is still values.local.yaml. This script only moves it around, and
no deploy path depends on it — every template carries the op inject one-liner.
EOF
}

# ---------------------------------------------------------------- dispatch

[[ $# -gt 0 ]] || { usage; exit 1; }
CMD="$1"; shift
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)          DRY_RUN=1 ;;
    --force)            FORCE=1 ;;
    --yes|-y)           ASSUME_YES=1 ;;
    --include-external) INCLUDE_EXTERNAL=1 ;;   # now the default; kept so old
                                                # invocations keep working
    --no-external)      INCLUDE_EXTERNAL=0 ;;
    --verify-only)      VERIFY_ONLY=1 ;;
    --files-only)       FILES_ONLY=1 ;;
    --keep)             KEEP="$2"; shift ;;
    --vault)            VAULT="$2"; shift ;;
    --item)             ITEM_OVERRIDE="$2"; shift ;;
    --from-file)        PUSH_FROM="$2"; shift ;;
    --from-stdin)       FROM_STDIN=1 ;;
    --reveal)           REVEAL=1 ;;
    -h|--help)          usage; exit 0 ;;
    -*)                 die "unknown option: $1" ;;
    *)                  ARGS+=("$1") ;;
  esac
  shift
done

SCRATCH="$(mktemp -d /dev/shm/secrets.XXXXXX)"
chmod 700 "$SCRATCH"

case "$CMD" in
  sync)    cmd_sync ;;
  check)   cmd_check ;;
  status)  cmd_status "${ARGS[@]+"${ARGS[@]}"}" ;;
  pull)    cmd_pull "${ARGS[@]+"${ARGS[@]}"}" ;;
  verify)  cmd_verify "${ARGS[@]+"${ARGS[@]}"}" ;;
  edit)    cmd_edit "${ARGS[@]+"${ARGS[@]}"}" ;;
  new)     cmd_new  "${ARGS[@]+"${ARGS[@]}"}" ;;
  show)    cmd_show "${ARGS[@]+"${ARGS[@]}"}" ;;
  import)  cmd_import "${ARGS[@]+"${ARGS[@]}"}" ;;
  push)    cmd_push "${ARGS[@]+"${ARGS[@]}"}" ;;
  backup)  cmd_backup ;;
  restore) cmd_restore ;;
  *)       usage; exit 1 ;;
esac
