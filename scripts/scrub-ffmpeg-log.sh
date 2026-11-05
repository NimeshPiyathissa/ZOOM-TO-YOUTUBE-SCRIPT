#!/usr/bin/env bash
# Removes YouTube stream keys that older versions wrote into ffmpeg logs.
#
#   scrub-ffmpeg-log.sh [--apply] [--dir DIR] [FILE...]
#
# DEFAULT IS A DRY RUN: it only COUNTS how many lines in each file contain an rtmp(s):// URL with a key. It never prints a
# matching line, a URL or a key - only file names and counts. Nothing is changed unless --apply is given.
#
# --apply rewrites each affected file in place (same owner/permissions, via a temp file in the same directory and an atomic
# rename) with the key replaced by <redacted>. NO backup copy is made on purpose: a backup would keep the very secret this
# removes. Run it while the encoder is stopped, or accept that lines appended during the rewrite are kept (it re-checks
# size and aborts that file if it changed under it).
#
# Default targets: $LOG_DIR/ffmpeg.log and rotated siblings (ffmpeg.log.*), where LOG_DIR is <APP_DIR>/logs, plus any FILEs.
# Exit status: 0 = nothing (left) to scrub, 1 = keys found (dry run) , 2 = usage/error.
set -u
umask 077

APPLY=0
DIR=""
FILES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dir) shift; DIR="${1:-}" ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) FILES+=("$1") ;;
  esac
  shift
done

if [ -z "$DIR" ] && [ ${#FILES[@]} -eq 0 ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  APP_DIR_GUESS="${APP_DIR:-$(dirname "$HERE")}"
  DIR="$APP_DIR_GUESS/logs"
fi
if [ -n "$DIR" ]; then
  [ -d "$DIR" ] || { echo "no such directory: $DIR" >&2; exit 2; }
  for f in "$DIR"/ffmpeg.log "$DIR"/ffmpeg.log.*; do [ -f "$f" ] && FILES+=("$f"); done
fi

# Same pattern as redact_stream_secrets in lib.sh: keep scheme/host/app, mask the key segment.
PATTERN='s#(rtmps?://[^/[:space:]'"'"'"]+/[^/[:space:]'"'"'"]+/)[^[:space:]'"'"'"]+#\1<redacted>#g'
KEYED='rtmps?://[^/[:space:]]+/[^/[:space:]]+/[^<[:space:]]'

total=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "skip (not a file): $f"; continue; }
  case "$f" in *.gz|*.xz|*.zst|*.bz2) echo "skip (compressed, scrub after decompressing): $f"; continue ;; esac
  # a URL whose key part is already <redacted> does not count
  hits=$(grep -E -c "$KEYED" "$f" || true)
  if [ "${hits:-0}" -eq 0 ]; then
    echo "clean : $f"
    continue
  fi
  total=$((total + hits))
  if [ "$APPLY" -ne 1 ]; then
    echo "WOULD SCRUB: $f  ($hits line(s) contain a stream key)"
    continue
  fi
  size_before=$(wc -c <"$f")
  tmp="$(mktemp "$(dirname "$f")/.scrub.XXXXXX")" || { echo "cannot create temp file next to $f" >&2; exit 2; }
  sed -E "$PATTERN" "$f" >"$tmp"
  size_now=$(wc -c <"$f")
  if [ "$size_now" != "$size_before" ]; then
    rm -f "$tmp"; echo "ABORTED $f: it changed while being scrubbed (is the encoder running?) - run again" >&2; continue
  fi
  chmod --reference="$f" "$tmp" 2>/dev/null || true
  chown --reference="$f" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$f"
  remaining=$(grep -E -c "$KEYED" "$f" || true)
  echo "scrubbed: $f  ($hits line(s) changed, $remaining still containing a key)"
done

if [ "$APPLY" -ne 1 ] && [ "$total" -gt 0 ]; then
  echo "DRY RUN: $total line(s) contain a stream key. Re-run with --apply to remove them. Nothing was changed."
  exit 1
fi
exit 0
