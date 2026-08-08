#!/usr/bin/env bash
# Move per-project secrets between 1Password, this checkout, the cluster, and
# the NAS. Replaces scripts/sync-local-values.sh (now a deprecated shim).
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

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VAULT="${VAULT:-homelab}"

# Cluster relay (claude-workspace). The pod runs as a cluster-admin SA, so it
# can already read every Secret in the cluster — including the rendered chart
# Secrets that carry most of these same values. Putting the bundle in a Secret
# therefore grants it nothing it did not already have, and buys a pull-based
# path: the pod refreshes itself after a restart without this laptop being up.
CLUSTER_NS="${CLUSTER_NS:-claude}"
CLUSTER_SECRET="${CLUSTER_SECRET:-selfhosted-secrets}"
POD_DEPLOY="${POD_DEPLOY:-claude-workspace}"
POD_REPO="${POD_REPO:-/home/node/code/selfhosted}"

# NAS backup. A dedicated share — deliberately NOT the games dataset, which
# games/romm mounts into the cluster read-only.
NAS_SHARE="${NAS_SHARE:-//192.168.4.36/backups}"
NAS_DIR="${NAS_DIR:-selfhosted-secrets}"
NAS_CREDS="${NAS_CREDS:-$HOME/.config/selfhosted/nas-creds}"
AGE_KEY="${AGE_KEY:-$HOME/.config/selfhosted/backup-age.key}"
AGE_RECIPIENTS="${AGE_RECIPIENTS:-${ROOT}/scripts/backup-recipients.txt}"
PAPER_MARKER="${PAPER_MARKER:-$HOME/.config/selfhosted/.paper-key-confirmed}"

# Secret files that are not values.local.yaml. apartment-watch reads criteria
# off disk via .Files.Get in templates/configmap.yaml, so it must be present
# before helm runs, exactly like a -f file.
EXTRA_FILES=("web/apartment-watch/criteria.yaml")

# infra/coredns-config/values.local.yaml is NOT a secret and must never be
# managed here. Its *presence* is what enables the cluster-wide DNS query log;
# its own .example says the tracked default must stay false and that deleting
# the file is how you close the window. Materializing it would silently reopen
# cluster DNS logging and restart CoreDNS.
EXCLUDE_FILES=("infra/coredns-config/values.local.yaml")

# Apps whose charts live in their own repo and deploy from a standalone clone
# (README §Conventions). Their secrets are outside this checkout entirely.
EXTERNAL_REPOS=("gamedex" "money" "smitele-bot")
# The laptop clones under ~/Code, the claude-workspace pod under ~/code. Pick
# whichever exists so the same command works in both without a flag.
if [[ -n "${EXTERNAL_BASE:-}" ]]; then :
elif [[ -d "$HOME/Code" ]]; then EXTERNAL_BASE="$HOME/Code"
elif [[ -d "$HOME/code" ]]; then EXTERNAL_BASE="$HOME/code"
else EXTERNAL_BASE="$HOME/Code"
fi

DRY_RUN=0
FORCE=0
INCLUDE_EXTERNAL=0
FROM_CLUSTER=0
VERIFY_ONLY=0
ASSUME_YES=0
FILES_ONLY=0
KEEP="${KEEP:-20}"
ITEM_OVERRIDE=""

die() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "==> $*"; }
skip() { echo "    skip: $*"; }

# ---------------------------------------------------------------- discovery

is_excluded() {
  local f="$1" e
  for e in "${EXCLUDE_FILES[@]}"; do [[ "$f" == "$e" ]] && return 0; done
  return 1
}

# Every managed secret file that currently exists, repo-relative.
discover_local() {
  local f
  while IFS= read -r f; do
    f="${f#./}"
    is_excluded "$f" || printf '%s\n' "$f"
  done < <(cd "$ROOT" && find . -name values.local.yaml -not -path './.git/*' | sort)
  for f in "${EXTRA_FILES[@]}"; do
    [[ -f "$ROOT/$f" ]] && printf '%s\n' "$f"
  done
  # Explicit: the loop's status is the last [[ -f ]], so a missing optional
  # extra file would otherwise make this function "fail".
  return 0
}

# Every chart that has been migrated, as "tpl<TAB>output" pairs.
discover_tpl() {
  local t out
  while IFS= read -r t; do
    t="${t#./}"
    out="$(tpl_output "$t")"
    is_excluded "$out" || printf '%s\t%s\n' "$t" "$out"
  done < <(cd "$ROOT" && find . \( -name 'values.local.tpl.yaml' -o -name 'criteria.tpl.yaml' \) \
             -not -path './.git/*' | sort)
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

need_op() {
  command -v op >/dev/null 2>&1 \
    || die "op not found. Install the 1Password CLI (>= 2.0), then: eval \$(op signin)"
  op whoami >/dev/null 2>&1 \
    || die "op is not signed in. Run: eval \$(op signin)"
}

# Materialize one template. Four guards, each covering a distinct failure, and
# an atomic rename so a bad run can never truncate a working file.
#
# op inject fails loudly on an unresolvable reference — non-zero exit, explicit
# message, nothing written. It does NOT substitute an empty string. The one way
# it can still produce emptiness is a vault field that exists and is empty,
# which is why only non-empty secrets get a ref (optional-empty values stay as
# literals in the template) and why `check` asserts non-emptiness.
inject_to() {
  local tpl="$1" out="$2" tmp
  # The temp file sits in the TARGET directory, not /dev/shm, so the final mv is
  # a same-filesystem rename and therefore atomic. A bad run can never leave a
  # half-written values.local.yaml where a working one used to be.
  tmp="$(mktemp "${ROOT}/$(dirname "$out")/.secrets.XXXXXX")"
  chmod 600 "$tmp"

  # No RETURN trap here — see scratch() below for why they misfire. Explicit
  # cleanup on every failure path instead.
  _fail() { rm -f "$tmp"; echo "FAIL: $1" >&2; return 1; }

  op inject -i "${ROOT}/${tpl}" -o "$tmp" -f >/dev/null 2>&1 \
    || { _fail "op inject failed for ${tpl}"; return 1; }
  if grep -q '{{ *op://' "$tmp"; then
    _fail "${tpl} left an unresolved reference"; return 1
  fi
  python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$tmp" 2>/dev/null \
    || { _fail "${tpl} produced invalid YAML (op inject is not YAML-aware)"; return 1; }

  mv "$tmp" "${ROOT}/${out}"
  chmod 600 "${ROOT}/${out}"
}

# Same, but to a caller-supplied path in tmpfs — for comparisons that must never
# touch the real file.
inject_probe() {
  local tpl="$1" out="$2"
  op inject -i "${ROOT}/${tpl}" -o "$out" -f >/dev/null 2>&1 || return 1
  grep -q '{{ *op://' "$out" && return 1
  python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$out" 2>/dev/null || return 1
  return 0
}

# One scratch root for the whole run, removed on any exit.
#
# Deliberately NOT a per-function `trap ... RETURN`: RETURN traps are not
# function-local, so they fire again in the caller — where the trap's variable
# is out of scope. Under `set -u` that aborts the script *after* the work is
# done and leaves a /dev/shm directory full of plaintext secrets behind.
#
# /dev/shm is tmpfs; / is ext4. Decrypted material must never touch a disk.
SCRATCH=""
cleanup_scratch() { [[ -n "$SCRATCH" && -d "$SCRATCH" ]] && rm -rf "$SCRATCH"; return 0; }
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

  local tmp rc=0; tmp="$(scratch)"
  local tpl out ref empty
  while IFS=$'\t' read -r tpl out; do
    [[ -n "$tpl" ]] || continue
    if ! inject_probe "$tpl" "$tmp/probe"; then
      echo "  UNRESOLVABLE  $tpl"; rc=1; continue
    fi
    # A ref that resolves to empty is the one way op inject can reintroduce the
    # silent-empty-secret failure this repo has been bitten by before.
    empty=0
    while IFS= read -r ref; do
      [[ -n "$ref" ]] || continue
      [[ -n "$(op read "$ref" 2>/dev/null)" ]] || { echo "  EMPTY FIELD   $ref"; empty=1; }
    done < <(grep -oE 'op://[^ }"]+' "${ROOT}/${tpl}" | sort -u)
    [[ $empty -eq 1 ]] && rc=1 || echo "  ok            $tpl"
  done < <(discover_tpl)
  rm -f "$tmp/probe"
  return $rc
}

cmd_status() {
  need_op
  local tmp; tmp="$(scratch)"
  printf '%-46s %s\n' "CHART" "STATE"
  local tpl out
  while IFS=$'\t' read -r tpl out; do
    [[ -n "$tpl" ]] || continue
    if [[ ! -f "${ROOT}/${out}" ]]; then
      printf '%-46s %s\n' "$out" "not-materialized"; continue
    fi
    if ! inject_probe "$tpl" "$tmp/probe"; then
      printf '%-46s %s\n' "$out" "UNRESOLVABLE"; continue
    fi
    if cmp -s "${ROOT}/${out}" "$tmp/probe"; then
      printf '%-46s %s\n' "$out" "in-sync"
    else
      # mtime vs vault updated_at is a heuristic; it cannot distinguish
      # concurrent edits. Say so rather than implying certainty.
      local upd loc dir
      dir="$(dirname "$out")"
      upd="$(op item get "$(item_title "$out")" --vault "$VAULT" --format json 2>/dev/null \
             | jq -r '.updated_at // empty')"
      loc="$(date -u -r "${ROOT}/${out}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '')"
      if [[ -n "$upd" && -n "$loc" && "$loc" > "$upd" ]]; then
        printf '%-46s %s\n' "$out" "DRIFT (local newer — probably needs push)"
      else
        printf '%-46s %s\n' "$out" "DRIFT (vault newer — probably needs pull)"
      fi
    fi
    rm -f "$tmp/probe"
  done < <(discover_tpl)

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
  [[ $pending -gt 0 ]] && echo && echo "${pending} file(s) still exist only on this machine."
  return 0
}

cmd_pull() {
  if [[ $FROM_CLUSTER -eq 1 ]]; then cmd_pull_cluster "$@"; return; fi
  need_op
  local tmp; tmp="$(scratch)"
  local tpl out n=0
  while IFS=$'\t' read -r tpl out; do
    [[ -n "$tpl" ]] || continue
    if [[ $# -gt 0 ]] && ! printf '%s\n' "$@" | grep -qF "$(dirname "$out")"; then continue; fi
    if [[ -f "${ROOT}/${out}" && $FORCE -eq 0 ]]; then
      if inject_probe "$tpl" "$tmp/probe" && ! cmp -s "${ROOT}/${out}" "$tmp/probe"; then
        skip "$out differs from the vault — inspect with 'status', then pull --force or push"
        rm -f "$tmp/probe"; continue
      fi
      rm -f "$tmp/probe"
    fi
    if [[ $DRY_RUN -eq 1 ]]; then echo "    would materialize $out"; n=$((n+1)); continue; fi
    inject_to "$tpl" "$out" && { echo "    $out"; n=$((n+1)); }
  done < <(discover_tpl)
  note "$n file(s)"
}

# One-time migration: store an existing file in 1Password verbatim, then write
# the template that reproduces it. Whole-file is lossless and mechanical, so it
# carries near-zero risk while delivering the entire recoverability benefit.
# Per-key conversion is where the judgment calls live; do it later, per chart.
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
      src="$f"
      [[ -f "$src" ]] || die "no such file: $src"
      [[ -n "$ITEM_OVERRIDE" ]] \
        || die "$src is outside the repo — pass --item <title> so it matches the submodule path"
      title="$ITEM_OVERRIDE"; label="$(field_label "$src")"
      tplabs="$(dirname "$src")/$([[ "$label" == values.local.yaml ]] && echo values.local.tpl.yaml || echo "${label%.yaml}.tpl.yaml")"
    else
      f="${f#"$ROOT"/}"; f="${f#./}"
      [[ -f "${ROOT}/${f}" ]] || die "no such file: $f"
      is_excluded "$f" && die "$f is deliberately excluded (see EXCLUDE_FILES)"
      src="${ROOT}/${f}"
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

    if [[ -s "$existing" ]]; then
      op item edit "$title" --vault "$VAULT" --template "$body_json" >/dev/null \
        || die "op item edit failed for ${title}"
    else
      op item create --vault "$VAULT" --template "$body_json" >/dev/null \
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
    if ! verify_abs "$tplabs" "$src"; then
      rm -f "$tplabs"
      die "$src does not round-trip — removed ${tplabs}; vault item ${title} left for inspection"
    fi
  done
}

# Gate 1 on absolute paths, so it also covers the standalone app clones.
verify_abs() {
  local tpl="$1" out="$2" tmp
  tmp="$(scratch)"
  op inject -i "$tpl" -o "$tmp/probe" -f >/dev/null 2>&1 \
    || { echo "    GATE1 FAIL (unresolvable)"; return 1; }
  grep -q '{{ *op://' "$tmp/probe" && { echo "    GATE1 FAIL (unresolved ref)"; return 1; }
  python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$tmp/probe" 2>/dev/null \
    || { echo "    GATE1 FAIL (invalid YAML)"; return 1; }
  if cmp -s "$out" "$tmp/probe"; then echo "    GATE1 byte-exact"; return 0; fi
  echo "    GATE1 FAIL (differs)"; return 1
}

# Gate 1: byte equality. Gate 2 (render equality) is cmd_verify.
verify_one() {
  local tpl="$1" out="$2" tmp rc=0
  tmp="$(scratch)"
  if ! inject_probe "$tpl" "$tmp/probe"; then rm -rf "$tmp"; echo "    GATE1 FAIL (unresolvable)"; return 1; fi
  if cmp -s "${ROOT}/${out}" "$tmp/probe"; then echo "    GATE1 byte-exact"; else echo "    GATE1 FAIL (differs)"; rc=1; fi
  rm -rf "$tmp"; return $rc
}

cmd_push() {
  need_op
  [[ $# -gt 0 ]] || die "usage: secrets.sh push <path> ..."
  local f title label
  for f in "$@"; do
    f="${f#"$ROOT"/}"; f="${f#./}"
    [[ -f "${ROOT}/${f}" ]] || die "no such file: $f"
    title="$(item_title "$f")"; label="$(field_label "$f")"
    if [[ $DRY_RUN -eq 1 ]]; then echo "    would push $f -> op://${VAULT}/${title}/${label}"; continue; fi
    note "pushing ${f} -> op://${VAULT}/${title}/${label}"
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

    python3 - "$ROOT/$f" "$title" "$label" "$existing" > "$bj" <<'PY'
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
    op item edit "$title" --vault "$VAULT" --template "$bj" >/dev/null \
      || die "push failed for ${f}"

    # PROVE IT LANDED. The whole reason this bug survived is that op reported
    # success while changing nothing, so a push that cannot show the vault now
    # reproduces the file is a failed push, not a finished one.
    local tpl_rel; tpl_rel="$(dirname "$f")/$([[ "$label" == values.local.yaml ]] \
      && echo values.local.tpl.yaml || echo "${label%.yaml}.tpl.yaml")"
    if [[ -f "${ROOT}/${tpl_rel}" ]]; then
      verify_one "$tpl_rel" "$f" >/dev/null \
        || die "pushed ${f} but the vault does not reproduce it — nothing is backed up"
    fi
  done
  # A backup can never be more than one change stale.
  [[ $DRY_RUN -eq 1 ]] || cmd_backup || true
}

# Gate 2 — render equality. The check that actually matters: an empty diff means
# the cluster would receive byte-identical manifests, proved entirely offline.
# No helm upgrade is needed to validate this migration.
cmd_verify() {
  need_op
  local tmp; tmp="$(scratch)"
  local tpl out dir rc=0
  while IFS=$'\t' read -r tpl out; do
    [[ -n "$tpl" ]] || continue
    if [[ $# -gt 0 ]] && ! printf '%s\n' "$@" | grep -qF "$(dirname "$out")"; then continue; fi
    dir="$(dirname "$out")"
    echo "  ${dir}"
    verify_one "$tpl" "$out" || rc=1
    if [[ ! -f "${ROOT}/${dir}/Chart.yaml" ]]; then
      echo "    GATE2 skipped (values-only project — render against the upstream chart by hand)"
      continue
    fi
    inject_probe "$tpl" "$tmp/probe" || { rc=1; continue; }
    if diff -q \
        <(helm template gate "${ROOT}/${dir}" -f "${ROOT}/${dir}/values.yaml" -f "${ROOT}/${out}" 2>/dev/null) \
        <(helm template gate "${ROOT}/${dir}" -f "${ROOT}/${dir}/values.yaml" -f "$tmp/probe" 2>/dev/null) >/dev/null; then
      echo "    GATE2 render-identical"
    else
      echo "    GATE2 FAIL — rendered manifests differ. Do NOT commit this template."
      rc=1
    fi
    rm -f "$tmp/probe"
  done < <(discover_tpl)
  return $rc
}

# ------------------------------------------------------- cluster relay

pack_bundle() {
  local dest="$1" tmp f name
  tmp="$(scratch)"
  mkdir -p "$tmp/files"
  while IFS= read -r f; do
    mkdir -p "$tmp/files/$(dirname "$f")"
    cp -p "${ROOT}/${f}" "$tmp/files/$f"
  done < <(discover_local)
  if [[ $INCLUDE_EXTERNAL -eq 1 ]]; then
    for name in "${EXTERNAL_REPOS[@]}"; do
      if [[ -f "${EXTERNAL_BASE}/${name}/values.local.yaml" ]]; then
        mkdir -p "$tmp/external/$name"
        cp -p "${EXTERNAL_BASE}/${name}/values.local.yaml" "$tmp/external/$name/"
      fi
    done
  fi
  # Exclude MANIFEST from its own listing: the shell creates it at redirect time,
  # so find would catch it mid-write and record a hash that never matches. A
  # wrong hash in the integrity record is worse than no entry.
  (cd "$tmp" && find . -type f ! -name MANIFEST | sed 's|^\./||' | sort | while read -r p; do
     printf '%s\t%s\t%s\n' "$(stat -c%s "$p")" "$(sha256sum "$p" | cut -d' ' -f1)" "$p"
   done) > "$tmp/MANIFEST"
  tar czf "$dest" -C "$tmp" .
  rm -rf "$tmp"
}

cmd_publish() {
  command -v kubectl >/dev/null || die "kubectl required"
  local tmp; tmp="$(scratch)"
  pack_bundle "$tmp/bundle.tar.gz"
  note "bundle: $(stat -c%s "$tmp/bundle.tar.gz") bytes, $(discover_local | wc -l) file(s)"
  if [[ $DRY_RUN -eq 1 ]]; then
    discover_local | sed 's/^/    /'
    echo "    would write secret/${CLUSTER_SECRET} in ns ${CLUSTER_NS}"
    return 0
  fi
  kubectl get namespace "$CLUSTER_NS" >/dev/null 2>&1 || die "namespace ${CLUSTER_NS} not found"
  kubectl create secret generic "$CLUSTER_SECRET" -n "$CLUSTER_NS" \
    --from-file=bundle.tar.gz="$tmp/bundle.tar.gz" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  note "published secret/${CLUSTER_SECRET} to ns ${CLUSTER_NS}"

  # Apply it immediately if the pod is up, so this is a strict improvement on
  # the old push. If it isn't, that's fine and not an error: the Secret is
  # durable, and the pod pulls for itself on next start — which is the whole
  # point of moving from push to pull.
  if kubectl -n "$CLUSTER_NS" get "deploy/${POD_DEPLOY}" >/dev/null 2>&1 \
     && kubectl -n "$CLUSTER_NS" exec "deploy/${POD_DEPLOY}" -c term -- test -d "$POD_REPO" >/dev/null 2>&1; then
    note "applying in ${POD_DEPLOY}"
    kubectl -n "$CLUSTER_NS" exec "deploy/${POD_DEPLOY}" -c term -- \
      bash -lc "cd '${POD_REPO}' && scripts/secrets.sh pull --from-cluster" \
      || echo "    pod-side pull failed — run it there by hand"
  else
    echo "    pod not reachable; it will pull on next start:"
    echo "      scripts/secrets.sh pull --from-cluster"
  fi
}

# Run inside the claude-workspace pod. No 1Password credentials are needed or
# wanted there: a Service Account is Teams-only, and putting the account
# password in the pod would expose every vault, not just this one.
cmd_pull_cluster() {
  command -v kubectl >/dev/null || die "kubectl required"
  local tmp; tmp="$(scratch)"
  kubectl get secret "$CLUSTER_SECRET" -n "$CLUSTER_NS" >/dev/null 2>&1 \
    || die "secret/${CLUSTER_SECRET} not found in ns ${CLUSTER_NS}. Run 'secrets.sh publish' on a machine with op."
  kubectl get secret "$CLUSTER_SECRET" -n "$CLUSTER_NS" \
    -o jsonpath='{.data.bundle\.tar\.gz}' | base64 -d > "$tmp/bundle.tar.gz"
  tar xzf "$tmp/bundle.tar.gz" -C "$tmp"
  if [[ $DRY_RUN -eq 1 ]]; then
    note "bundle contents:"; sed 's/^/    /' "$tmp/MANIFEST"; return 0
  fi
  local f n=0
  while IFS= read -r f; do
    f="${f#files/}"
    mkdir -p "${ROOT}/$(dirname "$f")"
    install -m600 "$tmp/files/$f" "${ROOT}/${f}"
    echo "    $f"; n=$((n+1))
  done < <(cd "$tmp" && find files -type f 2>/dev/null | sed 's|^files/||' | sort)
  # External app repos, if their clones exist alongside.
  local name
  for name in "${EXTERNAL_REPOS[@]}"; do
    if [[ -f "$tmp/external/$name/values.local.yaml" && -d "${EXTERNAL_BASE}/${name}" ]]; then
      install -m600 "$tmp/external/$name/values.local.yaml" "${EXTERNAL_BASE}/${name}/values.local.yaml"
      echo "    ${EXTERNAL_BASE}/${name}/values.local.yaml"; n=$((n+1))
    fi
  done
  note "$n file(s) restored from secret/${CLUSTER_SECRET}"
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
  elif command -v op >/dev/null 2>&1 && op whoami >/dev/null 2>&1 \
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
  cat <<'EOF'
Usage: scripts/secrets.sh <command> [options] [path...]

  check                    op reachable, every ref resolves AND is non-empty
  status [path...]         per-chart: in-sync / drift / not-materialized / NOT MIGRATED
  pull [path...]           materialize values.local.yaml from 1Password
  pull --from-cluster      materialize from the cluster bundle (use inside the pod)
  verify [path...]         Gate 1 (byte-exact) + Gate 2 (rendered manifests identical)
  import <path>...         one-time: store an existing file in the vault, write its template
  push <path>...           write local edits back to the vault, then back up
  publish                  pack every local secret into a Secret the pod can pull
  backup                   age-encrypt everything + vault dump, upload to the NAS, verify
  restore [--verify-only]  recover from the latest NAS archive

Options: --dry-run  --force  --yes  --include-external  --vault NAME
         --files-only (skip the vault dump)  --keep N (archive retention, default 20)

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
    --include-external) INCLUDE_EXTERNAL=1 ;;
    --from-cluster)     FROM_CLUSTER=1 ;;
    --verify-only)      VERIFY_ONLY=1 ;;
    --files-only)       FILES_ONLY=1 ;;
    --keep)             KEEP="$2"; shift ;;
    --vault)            VAULT="$2"; shift ;;
    --item)             ITEM_OVERRIDE="$2"; shift ;;
    -h|--help)          usage; exit 0 ;;
    -*)                 die "unknown option: $1" ;;
    *)                  ARGS+=("$1") ;;
  esac
  shift
done

SCRATCH="$(mktemp -d /dev/shm/secrets.XXXXXX)"
chmod 700 "$SCRATCH"

case "$CMD" in
  check)   cmd_check ;;
  status)  cmd_status "${ARGS[@]+"${ARGS[@]}"}" ;;
  pull)    cmd_pull "${ARGS[@]+"${ARGS[@]}"}" ;;
  verify)  cmd_verify "${ARGS[@]+"${ARGS[@]}"}" ;;
  import)  cmd_import "${ARGS[@]+"${ARGS[@]}"}" ;;
  push)    cmd_push "${ARGS[@]+"${ARGS[@]}"}" ;;
  publish) cmd_publish ;;
  backup)  cmd_backup ;;
  restore) cmd_restore ;;
  *)       usage; exit 1 ;;
esac
