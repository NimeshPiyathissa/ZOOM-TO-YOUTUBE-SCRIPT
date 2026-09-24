#!/usr/bin/env bash
# Atomically replaces current-source.env with new content, built entirely
# inside this script from NUL-delimited KEY/VALUE pairs read on stdin -
# mirrors write-env.sh exactly, for the same reasons (see that script for
# the full rationale). The only differences: this file holds source
# selection/options, which aren't secrets, so it's world-readable (644)
# rather than 600, and the values here (URLs, zoom-level, reload seconds,
# 0/1 flags) still get the same safe quoting - a webpage/direct URL is
# exactly the kind of dashboard-supplied value most likely to contain
# characters (&, ?, =, spaces) that would matter if this were ever
# re-evaluated by a shell instead of load_env_file().
#
# Wire protocol: KEY \0 VALUE \0 KEY \0 VALUE \0 ...
# Every value is written out as KEY="value", with backslash and double
# quote escaped, matching exactly what scripts/lib.sh's load_env_file()
# expects to read back. A value containing an embedded newline is refused
# outright; a CR is stripped.
#
# Called only by the dashboard, only via its narrow sudoers rule, with
# the pairs piped in (never as a command-line argument, so nothing lands
# in argv/process list/sudo logs). Keeps one backup.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_FILE="$APP_DIR/current-source.env"
TMP_FILE="$APP_DIR/current-source.env.tmp.$$"
STDIN_FILE="$APP_DIR/current-source.env.stdin.$$"

# Buffer stdin to a file first: the python script below is itself fed via
# a heredoc, which would otherwise steal python's stdin out from under
# the real NUL-delimited payload piped into this script.
cat > "$STDIN_FILE"
trap 'rm -f "$STDIN_FILE"' EXIT

python3 - "$STDIN_FILE" "$TMP_FILE" <<'PYEOF'
import re
import sys

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

in_path, out_path = sys.argv[1], sys.argv[2]
with open(in_path, "rb") as f:
    data = f.read()
fields = data.split(b"\x00")
if fields and fields[-1] == b"":
    fields = fields[:-1]
if len(fields) % 2 != 0:
    sys.exit("malformed input: odd number of NUL-delimited fields")
if not fields:
    sys.exit("refusing to write an empty current-source.env")

lines = []
seen = set()
for i in range(0, len(fields), 2):
    key = fields[i].decode("utf-8", errors="strict")
    val = fields[i + 1].decode("utf-8", errors="strict")
    if not KEY_RE.match(key):
        sys.exit(f"malformed input: bad key {key!r}")
    if key in seen:
        sys.exit(f"malformed input: duplicate key {key!r}")
    seen.add(key)
    if "\n" in val:
        sys.exit(f"refusing: value for {key} contains an embedded newline")
    val = val.replace("\r", "")
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    lines.append(f'{key}="{escaped}"')

with open(out_path, "w", newline="\n") as f:
    f.write("\n".join(lines) + "\n")
PYEOF

chmod 644 "$TMP_FILE"

if [[ -f "$SOURCE_FILE" ]]; then
  cp -p "$SOURCE_FILE" "$APP_DIR/current-source.env.bak"
  chmod 644 "$APP_DIR/current-source.env.bak"
fi

mv "$TMP_FILE" "$SOURCE_FILE"
echo "wrote $SOURCE_FILE"
