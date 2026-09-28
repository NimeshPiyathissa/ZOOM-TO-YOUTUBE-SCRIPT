#!/usr/bin/env bash
# Atomically writes the pipeline's *runtime* env file - the merge of
# non-secret .env keys with the vault's current decrypted secrets (see
# dashboard's app/runtime_env.py) - to a tmpfs-backed path instead of
# persistent disk, so a decrypted secret never touches a disk block, even
# transiently. Same wire protocol and KEY="value" rendering as
# write-env.sh (NUL-delimited pairs on stdin, parsed and escaped here,
# never `source`d/eval'd downstream - see that script's header for the
# full contract and scripts/lib.sh's load_env_file() for the reader side)
# - different (tmpfs) destination, and no .bak kept, since this file is
# fully regenerable from the vault + .env at any time, unlike .env itself.
# Called only by the dashboard, only via its narrow sudoers rule, with
# the pairs piped in (never as a command-line argument, so nothing
# sensitive lands in argv/process list/sudo logs).
set -euo pipefail

# XDG_RUNTIME_DIR is already tmpfs (systemd-logind), already used
# throughout this project's scripts (e.g. `manage.sh`'s invocations), and
# already scoped to this user - reuse it rather than provisioning a new
# mount. Falls back to /run/user/<uid> if the caller didn't export it (the
# dashboard's sudo invocation always will; this is defense in depth).
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ ! -d "$RUNTIME_DIR" ]]; then
  echo "runtime dir $RUNTIME_DIR does not exist - is a user session/linger active?" >&2
  exit 1
fi

ENV_DIR="$RUNTIME_DIR/zoom-stream"
ENV_FILE="$ENV_DIR/env"
TMP_FILE="$ENV_DIR/.env.tmp.$$"
STDIN_FILE="$ENV_DIR/.env.stdin.$$"

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

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
    sys.exit("refusing to write an empty runtime env")

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
mv "$TMP_FILE" "$ENV_FILE"
echo "wrote $ENV_FILE"
