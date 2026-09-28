#!/usr/bin/env bash
# Atomically replaces .env with new content, built entirely inside this
# script from NUL-delimited KEY/VALUE pairs read on stdin - the caller
# never sends pre-rendered .env text, so this is the one place that
# decides how a value gets quoted.
#
# Wire protocol: KEY \0 VALUE \0 KEY \0 VALUE \0 ...  NUL is the field
# separator (not newline) so a value may safely contain any byte,
# including a literal newline, without being confused for a record
# boundary - which is exactly the ambiguity that would otherwise let one
# config field forge a neighboring KEY=VALUE line.
#
# Every value is written out as KEY="value", with backslash and double
# quote escaped, matching exactly what scripts/lib.sh's load_env_file()
# expects to read back (see that function for the escaping convention).
# A value containing an embedded newline is refused outright, since this
# file format is one line per key. A CR is stripped (harmless artifact
# of Windows-originated paste, unlike a raw LF).
#
# Called only by the dashboard, only via its narrow sudoers rule, with
# the pairs piped in (never as a command-line argument, so nothing
# sensitive lands in argv/process list/sudo logs). Keeps one backup.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$APP_DIR/.env"
TMP_FILE="$APP_DIR/.env.tmp.$$"
STDIN_FILE="$APP_DIR/.env.stdin.$$"

# Buffer stdin to a file first: the python script below is itself fed via
# a heredoc, which would otherwise steal python's stdin out from under
# the real NUL-delimited payload piped into this script.
cat > "$STDIN_FILE"
chmod 600 "$STDIN_FILE"
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
    sys.exit("refusing to write an empty .env")

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

chmod 600 "$TMP_FILE"

if [[ -f "$ENV_FILE" ]]; then
  cp -p "$ENV_FILE" "$APP_DIR/.env.bak"
  chmod 600 "$APP_DIR/.env.bak"
fi

mv "$TMP_FILE" "$ENV_FILE"
echo "wrote $ENV_FILE"
