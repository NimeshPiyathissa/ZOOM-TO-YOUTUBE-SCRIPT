#!/usr/bin/env bash
# Writes a watermark image's raw bytes (read on stdin - never a
# command-line argument) to a fixed, zoombot-owned path so ffmpeg's
# overlay filter (see lib.sh's build_watermark_filter()) can read it.
# Called only by the dashboard, only via its narrow sudoers rule. Takes
# the target extension as $1 (validated - the dashboard already restricts
# uploads to png/jpg/jpeg/webp before ever reaching here, this is defense
# in depth, not the primary check) so re-uploading replaces the previous
# image outright rather than accumulating orphaned files - there's only
# ever one active watermark image.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATERMARK_DIR="$APP_DIR/watermarks"
EXT="${1:-}"

case "$EXT" in
  .png|.jpg|.jpeg|.webp) ;;
  *) echo "refusing: unsupported extension '$EXT'" >&2; exit 1 ;;
esac

mkdir -p "$WATERMARK_DIR"
chmod 755 "$WATERMARK_DIR"

TARGET="$WATERMARK_DIR/logo${EXT}"
TMP_FILE="$WATERMARK_DIR/.logo.tmp.$$"

cat > "$TMP_FILE"
if [[ ! -s "$TMP_FILE" ]]; then
  rm -f "$TMP_FILE"
  echo "refusing to write an empty watermark image" >&2
  exit 1
fi

# Drop any other logo.* from a previous upload with a different
# extension, so build_watermark_filter() never finds two candidates.
rm -f "$WATERMARK_DIR"/logo.*
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$TARGET"
echo "$TARGET"
