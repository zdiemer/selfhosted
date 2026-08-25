#!/usr/bin/env bash
# Extract embedded text subtitles to sidecar .srt files next to the media.
#
# Why: Jellyfin extracts embedded subtitles lazily, on the first request, by
# shelling out to ffmpeg against the NFS mount. That measured ~11s per episode
# here, and the stall is what makes subtitles "not show up" or vanish after a
# seek — the player gives up before the demux finishes. Sidecar files are read
# straight off disk with no ffmpeg in the path.
#
# It also survives a config PVC rebuild, which /config/data/subtitles does not.
#
# Runs INSIDE the jellyfin pod: that is where both jellyfin-ffmpeg and the
# /media NFS mount already exist. Nothing is installed on the nodes.
#
#   ./extract-subtitles.sh                      # audit only (default) — writes nothing
#   ./extract-subtitles.sh --apply              # write sidecars for English
#   ./extract-subtitles.sh --langs eng,spa --apply
#   ./extract-subtitles.sh --all-tracks         # include duplicate tracks per language
#   ./extract-subtitles.sh --apply --limit 5
#   ./extract-subtitles.sh --as-job            # full run as a background Job
#   ./extract-subtitles.sh --path /media/tv/FROM --apply     # one show
#   ./extract-subtitles.sh --max-gb 8 --apply                # skip the big remuxes
#
# COST — read this before a full run. ffmpeg has to demux the whole file to
# reach the subtitle packets (they are interleaved throughout a Matroska, not
# seekable to), so extraction time tracks FILE SIZE, not subtitle length.
# Measured here: a 1.3GB episode takes ~32s; a 22GB REMUX takes minutes. Across
# 1.8TB that is roughly 12 hours of continuous NFS reads, which will compete
# with playback on the same mount and the same node the whole time.
#
# So scope it: --path per show, or --max-gb to skip the movie remuxes (a
# 22-minute read to save an 11-second stall on a film watched once is a bad
# trade; a series you watch 8 episodes of back to back is a good one). And run
# it when nobody is watching.
#
# Defaults are narrow on purpose. A full unfiltered run over this library plans
# ~4,300 files — REMUX releases routinely carry 15-45 subtitle languages, and a
# .srt for every one of them is clutter in the library and noise in the Jellyfin
# track picker. Default: English only, first track per language.
#
# Audit mode is the default deliberately: this writes into the live library,
# and the run is long enough that you want to see the plan first.
#
# Naming follows Jellyfin's sidecar convention, so the files are picked up with
# no library setting to change:
#   Episode.mkv -> Episode.eng.srt          first text track for a language
#                  Episode.eng.sdh.srt      hearing-impaired variant
#                  Episode.eng.forced.srt   forced-only track
#                  Episode.eng.2.srt        extra tracks (--all-tracks only)
#
# Only text subtitles are extracted (subrip/ass/ssa/mov_text). PGS and VOBSUB
# are bitmap formats — they cannot become .srt and are skipped; those still
# need burn-in to display.

set -euo pipefail

NAMESPACE="${NAMESPACE:-media}"
APPLY=0
LIMIT=0
LANGS="${LANGS:-eng}"
ALL_TRACKS=0
ROOTS="/media/tv /media/movies"
MAX_GB=0
AS_JOB=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --langs) LANGS="$2"; shift 2 ;;
    --all-tracks) ALL_TRACKS=1; shift ;;
    --path) ROOTS="$2"; shift 2 ;;
    --as-job) AS_JOB=1; shift ;;
    --max-gb) MAX_GB="$2"; shift 2 ;;
    # Print the header block, whatever length it grows to.
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# --as-job: hand the whole run to a Job instead of this shell. Required for a
# full pass — see the header, and subtitle-extract-job.yaml for why an exec is
# the wrong lifecycle for something that takes half a day.
if [[ "$AS_JOB" == "1" ]]; then
  HERE="$(cd "$(dirname "$0")" && pwd)"
  if kubectl -n "$NAMESPACE" get job jellyfin-subtitle-extract >/dev/null 2>&1; then
    echo "job jellyfin-subtitle-extract already exists." >&2
    echo "  logs:   kubectl -n ${NAMESPACE} logs -f job/jellyfin-subtitle-extract" >&2
    echo "  cancel: kubectl -n ${NAMESPACE} delete job jellyfin-subtitle-extract" >&2
    exit 1
  fi
  # Rebuilt every time so the Job always runs the current script.
  kubectl -n "$NAMESPACE" create configmap jellyfin-subtitle-extract \
    --from-file=extract-subtitles.remote.sh="${HERE}/extract-subtitles.remote.sh" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f "${HERE}/subtitle-extract-job.yaml"
  echo
  echo "==> started. This runs for hours; it is safe to close this shell."
  echo "    logs:   kubectl -n ${NAMESPACE} logs -f job/jellyfin-subtitle-extract"
  echo "    cancel: kubectl -n ${NAMESPACE} delete job jellyfin-subtitle-extract"
  exit 0
fi

POD="$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=jellyfin \
        -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$POD" ]] || { echo "no jellyfin pod found in ns/${NAMESPACE}" >&2; exit 1; }

echo "==> pod ${POD} (ns/${NAMESPACE})  apply=${APPLY}  langs=${LANGS}  all-tracks=${ALL_TRACKS}  limit=${LIMIT:-none}"

# The whole scan runs in one exec: a per-file round trip would be dominated by
# kubectl startup, and ffprobe over NFS is the slow part regardless.
kubectl -n "$NAMESPACE" exec -i "$POD" -- env \
  APPLY="$APPLY" LIMIT="$LIMIT" LANGS="$LANGS" ALL_TRACKS="$ALL_TRACKS" \
# The body lives in extract-subtitles.remote.sh so that this wrapper and the
# Job manifest run byte-identical code — a copy in each would drift.
kubectl -n "$NAMESPACE" exec -i "$POD" -- env \
  APPLY="$APPLY" LIMIT="$LIMIT" LANGS="$LANGS" ALL_TRACKS="$ALL_TRACKS" \
  ROOTS="$ROOTS" MAX_GB="$MAX_GB" sh -s < "$(dirname "$0")/extract-subtitles.remote.sh"

echo
if [[ "$APPLY" == "0" ]]; then
  echo "==> audit only; nothing was written. Re-run with --apply to extract."
else
  echo "==> done. Jellyfin picks sidecars up on the next library scan"
  echo "    (Dashboard -> Scheduled Tasks -> Scan Media Library)."
fi
