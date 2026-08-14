#!/usr/bin/env bash
# Move the submodule pins to what is actually deployed.
#
# WHY THIS IS NOT `git submodule update --remote`
# The README says the pin here records "the deployed commit". Nothing enforced
# that: the pins moved by hand, whenever someone remembered, and a plain
# fast-forward would quietly redefine them as "whatever is on the branch" —
# including work that was pushed but never shipped. Then this repo would claim
# the cluster runs code it has never run, which is worse than a stale pin
# because it reads as authoritative.
#
# So each candidate is checked against the cluster before its pin moves: read
# the chart's image repository and tag at the candidate commit, ask the API
# server what is actually running, and only move the pin when they agree.
#
# FOUR STRATEGIES, because the repos are not alike:
#
#   cluster  compare the chart's image tag with the running pods.  (the default)
#   tag      pin the newest v* tag. whatnowgg deploys with docker compose to a
#            VPS and never appears in this cluster, so there is nothing to ask.
#   head     pin the branch head and say plainly that it could not be verified.
#            talaria pins every image to `latest`, so a tag comparison would
#            compare "latest" with "latest" and prove nothing.
#   skip     web/old-diemer-codes/site is a frozen 2019 archive we do not touch.
#
# Each entry is  <submodule path>:<chart dir within the repo>:<strategy>.
SUBMODULES=(
  "games/gamedex:.:cluster"
  "finance/money:.:cluster"
  "discord/smitele-bot:.:cluster"
  "infra/sms-relay:.:cluster"
  "web/diemer-codes:.:cluster"
  "web/whatnowgg:deploy/chart:tag"
  "web/talaria:helm/talaria:head"
  "web/old-diemer-codes/site::skip"
)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCKER="${ROOT}/scripts/submodules-lock.sh"
cd "$ROOT"

DO_COMMIT=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --commit) DO_COMMIT=1 ;;
    --force)  FORCE=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/sync-submodules.sh [--commit] [--force]

  (no flags)  report what would move, change nothing
  --commit    move the verified pins and make a local commit (never pushes)
  --force     move a pin even when the cluster does not confirm it

A pin only moves when the chart's image tag at the candidate commit matches the
image actually running in the cluster, so the pin keeps meaning "what shipped".
EOF
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

die() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "==> $*"; }

# Every image running in the cluster, once. Pods rather than Deployments: this
# is about what IS running, not what was asked for.
LIVE_IMAGES=""
load_live_images() {
  command -v kubectl >/dev/null 2>&1 || { note "kubectl not found — cluster checks will be skipped"; return 0; }
  # Running pods only. Without the field selector this also sees Completed
  # CronJob pods, which linger for days at whatever image they ran with — so a
  # months-old backfill job was enough to make smitele-bot look like it was
  # running 1.5.6 when every Deployment in the namespace was on 1.9.8, and the
  # pin refused to advance for a submodule that was correctly deployed.
  #
  # That failure is quiet in the wrong direction: it does not move a pin it
  # should not, it *declines* to move one it should, and the repo keeps
  # claiming the cluster runs older code than it does.
  LIVE_IMAGES="$(kubectl get pods -A --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{range .spec.initContainers[*]}{.image}{"\n"}{end}{end}' 2>/dev/null | sort -u || true)"
  [[ -n "$LIVE_IMAGES" ]] || note "could not read pods from the cluster — checks will report unverified"
}

# The tag the cluster is running for an image repository, or empty.
live_tag_for() {
  local repo="$1"
  printf '%s\n' "$LIVE_IMAGES" | awk -F: -v r="$repo" '$1==r {print $2}' | head -1
}

# repository<TAB>tag from a chart's values.yaml at a given commit.
chart_image_at() {
  local p="$1" chart="$2" rev="$3" path
  path="values.yaml"; [[ "$chart" != "." && -n "$chart" ]] && path="${chart}/values.yaml"
  git -C "$p" show "${rev}:${path}" 2>/dev/null | python3 -c '
import sys, yaml
try:
    v = yaml.safe_load(sys.stdin) or {}
except Exception:
    sys.exit(1)
img = v.get("image") or {}
repo, tag = img.get("repository"), img.get("tag")
if not repo:
    sys.exit(1)
# No single quotes anywhere in here: this whole program is inside a
# single-quoted shell word, and one would end it mid-expression.
print(repo + "\t" + ("" if tag is None else str(tag)))
' 2>/dev/null || true
}

short() { git -C "$1" rev-parse --short "$2" 2>/dev/null || echo '?'; }

MOVED=()
REPORT=()

sync_one() {
  local path="$1" chart="$2" strategy="$3"
  local branch current target repo tag live desc

  [[ -e "${path}/.git" ]] || { REPORT+=("$(printf '%-28s %s' "$path" 'not initialised — git submodule update --init')"); return 0; }

  if [[ "$strategy" == "skip" ]]; then
    REPORT+=("$(printf '%-28s %s' "$path" 'skipped (frozen archive)')"); return 0
  fi

  branch="$(git config -f .gitmodules --get "submodule.${path}.branch" 2>/dev/null || echo main)"
  # The PIN, from the index — not the worktree's HEAD. They disagree whenever
  # this clone has pulled a pin move it never checked out, and the worktree is
  # then the older of the two. Reading HEAD there let a stale checkout defeat
  # the never-travel-backwards guard below and re-pin an already-recorded
  # commit. scripts/submodules-refresh.sh keeps them equal; this is the belt.
  current="$(git rev-parse "HEAD:${path}" 2>/dev/null || git -C "$path" rev-parse HEAD)"

  git -C "$path" fetch --quiet --tags origin "$branch" 2>/dev/null \
    || { REPORT+=("$(printf '%-28s %s' "$path" 'fetch failed')"); return 0; }

  case "$strategy" in
    tag)
      target="$(git -C "$path" tag -l 'v*' --sort=-v:refname | head -1)"
      [[ -n "$target" ]] || { REPORT+=("$(printf '%-28s %s' "$path" 'no v* tag found')"); return 0; }
      target="$(git -C "$path" rev-list -n1 "$target")"
      desc="tag $(git -C "$path" describe --tags --abbrev=0 "$target" 2>/dev/null || echo '?') (not in this cluster)"
      ;;
    *)
      target="$(git -C "$path" rev-parse "origin/${branch}")"
      desc=""
      ;;
  esac

  if [[ "$current" == "$target" ]]; then
    REPORT+=("$(printf '%-28s %s' "$path" 'up to date')"); return 0
  fi

  # A pin must never travel backwards. web/whatnowgg is pinned several commits
  # PAST its newest tag (v1.0.98-12-g834e537 as of this writing), so the tag
  # strategy would otherwise "advance" it to an older commit and quietly
  # un-record a deploy. Only move when the candidate is genuinely ahead.
  if git -C "$path" merge-base --is-ancestor "$target" "$current" 2>/dev/null; then
    REPORT+=("$(printf '%-28s %s' "$path" "pin is already ahead of $(short "$path" "$target") — left alone")")
    return 0
  fi

  local move=0
  case "$strategy" in
    cluster)
      local ci; ci="$(chart_image_at "$path" "$chart" "$target")"
      if [[ -z "$ci" ]]; then
        desc="no image.repository in the chart — unverified"
      else
        repo="${ci%%$'\t'*}"; tag="${ci#*$'\t'}"
        live="$(live_tag_for "$repo")"
        if [[ -z "$live" ]]; then
          desc="${repo} is not running in this cluster — unverified"
        elif [[ "$live" == "$tag" ]]; then
          desc="cluster runs ${repo}:${live} = chart"; move=1
        else
          desc="ahead of cluster (chart ${tag}, cluster ${live}) — not pinned"
        fi
      fi
      ;;
    head)
      desc="branch head; images are pinned to :latest so nothing can be verified"
      move=1
      ;;
    tag)
      move=1
      ;;
  esac

  [[ $FORCE -eq 1 ]] && move=1

  if [[ $move -eq 1 ]]; then
    REPORT+=("$(printf '%-28s %s -> %s  %s' "$path" "$(short "$path" "$current")" "$(short "$path" "$target")" "$desc")")
    MOVED+=("${path}:${target}:${desc}")
  else
    REPORT+=("$(printf '%-28s %s -> %s  %s' "$path" "$(short "$path" "$current")" "$(short "$path" "$target")" "$desc")")
  fi
}

load_live_images

# Checkout needs write access, which the lock deliberately removes. Unlock for
# the duration and lock again on the way out, including on failure — a run that
# died halfway must not leave the worktrees writable.
relock() { [[ -x "$LOCKER" ]] && "$LOCKER" lock >/dev/null 2>&1 || true; }
if [[ -x "$LOCKER" ]]; then trap relock EXIT INT TERM; "$LOCKER" unlock >/dev/null 2>&1 || true; fi

for entry in "${SUBMODULES[@]}"; do
  IFS=':' read -r p c s <<<"$entry"
  sync_one "$p" "$c" "$s"
done

printf '%s\n' "${REPORT[@]}"

if [[ ${#MOVED[@]} -eq 0 ]]; then
  note "nothing to move"
  exit 0
fi

if [[ $DO_COMMIT -eq 0 ]]; then
  echo
  note "${#MOVED[@]} pin(s) would move — rerun with --commit"
  exit 0
fi

BODY=""
for m in "${MOVED[@]}"; do
  p="${m%%:*}"; rest="${m#*:}"; target="${rest%%:*}"; desc="${rest#*:}"
  git -C "$p" checkout --quiet --detach "$target"
  git add "$p"
  # The newline goes outside the substitution: $(...) strips trailing newlines,
  # so building the line with a \n inside it ran every pin onto one line.
  BODY+="$(printf '  %-28s %s  %s' "$p" "$(short "$p" "$target")" "$desc")"$'\n'
done

git diff --cached --quiet -- "${MOVED[@]%%:*}" && { note "pins already matched — nothing staged"; exit 0; }

git commit --quiet -m "submodules: record what the cluster is running

$(printf '%s' "$BODY")

Moved by scripts/sync-submodules.sh, which only advances a pin once the chart's
image tag at that commit matches the image the cluster is actually running."
note "committed ${#MOVED[@]} pin(s) — not pushed"
