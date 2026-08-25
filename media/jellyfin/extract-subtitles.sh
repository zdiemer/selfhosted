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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --langs) LANGS="$2"; shift 2 ;;
    --all-tracks) ALL_TRACKS=1; shift ;;
    # Print the header block, whatever length it grows to.
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

POD="$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=jellyfin \
        -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$POD" ]] || { echo "no jellyfin pod found in ns/${NAMESPACE}" >&2; exit 1; }

echo "==> pod ${POD} (ns/${NAMESPACE})  apply=${APPLY}  langs=${LANGS}  all-tracks=${ALL_TRACKS}  limit=${LIMIT:-none}"

# The whole scan runs in one exec: a per-file round trip would be dominated by
# kubectl startup, and ffprobe over NFS is the slow part regardless.
kubectl -n "$NAMESPACE" exec -i "$POD" -- env \
  APPLY="$APPLY" LIMIT="$LIMIT" LANGS="$LANGS" ALL_TRACKS="$ALL_TRACKS" sh -s <<'REMOTE'
set -eu

FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe
FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg

list=$(mktemp); sfile=$(mktemp)
trap 'rm -f "$list" "$sfile"' EXIT

find /media/tv /media/movies -type f \( -name '*.mkv' -o -name '*.mp4' \) 2>/dev/null \
  | sort > "$list"

planned=0; written=0; failed=0; skipped=0; bitmap=0; seen=0; otherlang=0; dupe=0

# ",eng," so a substring test can't match "en" inside "eng" or "ger" inside "ge".
langset=",$(echo "$LANGS" | tr -d ' '),"

# Redirected, not piped: a `find | while` runs the loop in a subshell and every
# counter below would be discarded when it exits.
while IFS= read -r f; do

  if [ "$LIMIT" -gt 0 ] && [ "$seen" -ge "$LIMIT" ]; then break; fi
  seen=$((seen + 1))

  base="${f%.*}"

  # ffprobe emits fields in section order, NOT the order requested:
  #   index, codec_name, forced, hearing_impaired, language
  "$FFPROBE" -v error -select_streams s \
      -show_entries stream=index,codec_name:stream_disposition=forced,hearing_impaired:stream_tags=language \
      -of csv=p=0:nk=1 -- "$f" 2>/dev/null > "$sfile" || : > "$sfile"
  [ -s "$sfile" ] || continue

  lang_seen=""
  while IFS=, read -r idx codec forced hi lang; do
    [ -n "${idx:-}" ] || continue

    case "$codec" in
      subrip|ass|ssa|mov_text|webvtt) : ;;
      hdmv_pgs_subtitle|dvd_subtitle|dvb_subtitle)
        bitmap=$((bitmap + 1)); continue ;;
      *) continue ;;
    esac

    lang="${lang:-und}"

    if [ "$langset" != ",all," ] && ! echo "$langset" | grep -q ",${lang},"; then
      otherlang=$((otherlang + 1)); continue
    fi

    # forced/hearing-impaired are meaningful to Jellyfin. A second unflagged
    # track of the same language gets its stream index instead — stable, and it
    # keeps the two from colliding on one filename. (These files carry two
    # unflagged English SRTs, so this is the common case here, not an edge one.)
    suffix=""
    if [ "${forced:-0}" = "1" ]; then
      suffix=".forced"
    elif [ "${hi:-0}" = "1" ]; then
      suffix=".sdh"
    elif echo "$lang_seen" | grep -q "|${lang}|"; then
      # A second unflagged track of the same language. Usually the SDH variant,
      # but nothing in the file says so — these releases leave the disposition
      # bits clear — so it is not guessed at. Skipped unless asked for.
      if [ "$ALL_TRACKS" = "0" ]; then
        dupe=$((dupe + 1)); continue
      fi
      suffix=".${idx}"
    fi
    lang_seen="${lang_seen}|${lang}|"

    out="${base}.${lang}${suffix}.srt"

    if [ -f "$out" ]; then
      skipped=$((skipped + 1)); continue
    fi

    planned=$((planned + 1))
    if [ "$APPLY" = "1" ]; then
      # -c:s srt converts ass/ssa/mov_text to SubRip; for a subrip source it is
      # a copy in all but name. Write to .part first so an interrupted run never
      # leaves a truncated sidecar that the next run then skips as "exists".
      if "$FFMPEG" -v error -y -i "$f" -map "0:${idx}" -c:s srt -- "${out}.part" 2>/dev/null \
         && [ -s "${out}.part" ]; then
        mv -- "${out}.part" "$out"
        written=$((written + 1))
        echo "WROTE  $out"
      else
        rm -f -- "${out}.part"
        failed=$((failed + 1))
        echo "FAIL   $out"
      fi
    else
      echo "PLAN   $out  (stream ${idx}, ${codec})"
    fi
  done < "$sfile"
done < "$list"

echo "---"
echo "files scanned:    ${seen}"
echo "sidecars planned: ${planned}"
echo "already present:  ${skipped}"
echo "other languages:  ${otherlang} (filtered out by --langs)"
echo "duplicate tracks: ${dupe} (use --all-tracks to include)"
if [ "$APPLY" = "1" ]; then echo "written:          ${written}  (failed: ${failed})"; fi
echo "bitmap streams skipped (cannot become .srt, need burn-in): ${bitmap}"
REMOTE

echo
if [[ "$APPLY" == "0" ]]; then
  echo "==> audit only; nothing was written. Re-run with --apply to extract."
else
  echo "==> done. Jellyfin picks sidecars up on the next library scan"
  echo "    (Dashboard -> Scheduled Tasks -> Scan Media Library)."
fi
