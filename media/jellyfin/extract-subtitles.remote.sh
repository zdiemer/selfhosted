set -eu

FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe
FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg

list=$(mktemp); sfile=$(mktemp)
trap 'rm -f "$list" "$sfile"' EXIT

# shellcheck disable=SC2086 -- ROOTS is a deliberate multi-path word split
find $ROOTS -type f \( -name '*.mkv' -o -name '*.mp4' \) 2>/dev/null \
  | sort > "$list"

planned=0; written=0; failed=0; skipped=0; bitmap=0; seen=0; otherlang=0; dupe=0; toobig=0

# ",eng," so a substring test can't match "en" inside "eng" or "ger" inside "ge".
langset=",$(echo "$LANGS" | tr -d ' '),"

# Redirected, not piped: a `find | while` runs the loop in a subshell and every
# counter below would be discarded when it exits.
while IFS= read -r f; do

  if [ "$LIMIT" -gt 0 ] && [ "$seen" -ge "$LIMIT" ]; then break; fi
  seen=$((seen + 1))

  # Size gate before ffprobe: the whole cost of this script is proportional to
  # bytes read, so the cheapest file is the one never opened.
  if [ "$MAX_GB" -gt 0 ]; then
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if [ "$sz" -gt $(( MAX_GB * 1024 * 1024 * 1024 )) ]; then
      toobig=$((toobig + 1)); continue
    fi
  fi

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
      # -f srt is required, not redundant: the .part suffix defeats ffmpeg's
      # extension-based muxer detection and it refuses to open the output.
      if "$FFMPEG" -v error -y -i "$f" -map "0:${idx}" -c:s srt -f srt -- "${out}.part" 2>/dev/null \
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
echo "over --max-gb:    ${toobig} (skipped unopened)"
echo "other languages:  ${otherlang} (filtered out by --langs)"
echo "duplicate tracks: ${dupe} (use --all-tracks to include)"
if [ "$APPLY" = "1" ]; then echo "written:          ${written}  (failed: ${failed})"; fi
echo "bitmap streams skipped (cannot become .srt, need burn-in): ${bitmap}"
