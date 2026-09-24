"""Round-trip tests for the KEY="value" env-file format three independent
implementations must agree on:

  1. write-env.sh's embedded Python writer (and write-source.sh's/
     write-runtime-env.sh's identical copies) - escapes \\ and " when
     turning NUL-delimited wire-format pairs into KEY="value" lines.
  2. scripts/lib.sh's load_env_file() (bash) - the reader every pipeline
     script actually uses.
  3. app/env_store.py's _unquote_env_value() (Python) - the reader the
     dashboard uses to show the current config back to the operator.

There was no test tying these three together before this file - each was
only ever exercised indirectly, so a format disagreement between the bash
reader and the Python reader (or between either reader and what the
writer actually produces) could have shipped unnoticed. Skips (not fails)
if a real `python3` and `bash` aren't available to actually run the
scripts - see the two skip guards below - so this still runs for real in
CI (Linux) without breaking a Windows dev machine where `python3` may
resolve to a non-functional Microsoft Store stub."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import env_store

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WRITE_ENV_SCRIPT = REPO_ROOT / "scripts" / "write-env.sh"
LIB_SH = REPO_ROOT / "scripts" / "lib.sh"

TRICKY_VALUES = {
    "PLAIN": "hello world",
    "EMPTY": "",
    "WITH_QUOTE": 'she said "hi"',
    "WITH_BACKSLASH": r"C:\path\to\thing",
    "WITH_BOTH": r'a "quoted" \path\ with "both"',
    "WITH_DOLLAR": "$HOME and `whoami` and $(pwd)",
    "WITH_AMPERSAND": "a & b && c",
    "WITH_SEMICOLON": "a; rm -rf /; b",
    "WITH_HASH": "not a # comment",
    "WITH_UNICODE": "café ☕ 日本語",
    "WITH_SPACES_ONLY": "   ",
    "REAL_ZOOM_LINK": "https://zoom.us/j/1234567890?pwd=AbC.123-xyz_1",
}


def _python3_actually_works() -> bool:
    exe = shutil.which("python3")
    if not exe:
        return False
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, timeout=5)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _bash_available() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(
    not (_python3_actually_works() and _bash_available() and WRITE_ENV_SCRIPT.is_file()),
    reason="needs a real python3 + bash + scripts/write-env.sh (skipped on a dev machine where "
           "python3 is a non-functional stub - runs for real in CI)",
)


def _run_write_env(tmp_path: Path, pairs: dict[str, str]) -> Path:
    """Copies write-env.sh into a scratch APP_DIR (it derives its own
    target path from its own location: "$(dirname .../..)/.env") and
    pipes the same NUL-delimited wire format env_store.py's
    write_updates() actually sends it."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_copy = scripts_dir / "write-env.sh"
    script_copy.write_bytes(WRITE_ENV_SCRIPT.read_bytes())
    script_copy.chmod(0o755)

    content = env_store._encode_env_pairs(pairs)
    proc = subprocess.run(
        ["bash", str(script_copy)], input=content, capture_output=True, timeout=15, cwd=str(scripts_dir),
    )
    assert proc.returncode == 0, f"write-env.sh failed: {proc.stderr.decode(errors='replace')}"
    return tmp_path / ".env"


def _read_via_bash_load_env_file(env_file: Path) -> dict[str, str]:
    """Sources the real lib.sh, calls the real load_env_file() on the
    file write-env.sh actually produced, and prints every TRICKY_VALUES
    key back out - NUL-delimited so a value containing a real newline
    can't be misread as a record boundary here either."""
    keys = list(TRICKY_VALUES.keys())
    print_cmds = "".join(f'printf "%s\\0" "${{{k}:-}}"\n' for k in keys)
    # lib.sh isn't written to be sourced standalone (it expects a real
    # APP_DIR/.env and DISPLAY at import time) - extract just the
    # load_env_file function body instead of fighting that.
    lib_text = LIB_SH.read_text(encoding="utf-8")
    start = lib_text.index("load_env_file() {")
    end = lib_text.index("\n}", start) + 2
    func_src = lib_text[start:end]
    script = f"""
set -euo pipefail
{func_src}
load_env_file "{env_file.as_posix()}"
{print_cmds}
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, timeout=15)
    assert proc.returncode == 0, f"load_env_file() failed: {proc.stderr.decode(errors='replace')}"
    parts = proc.stdout.split(b"\x00")
    return {k: parts[i].decode("utf-8") for i, k in enumerate(keys)}


def test_write_env_then_bash_load_env_file_roundtrips(tmp_path):
    env_file = _run_write_env(tmp_path, TRICKY_VALUES)
    recovered = _read_via_bash_load_env_file(env_file)
    for key, original in TRICKY_VALUES.items():
        assert recovered[key] == original, f"{key}: wrote {original!r}, bash reader got {recovered[key]!r}"


def test_write_env_then_python_unquote_roundtrips(tmp_path):
    env_file = _run_write_env(tmp_path, TRICKY_VALUES)
    text = env_file.read_text(encoding="utf-8")
    recovered: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        k, _, v = line.partition("=")
        recovered[k.strip()] = env_store._unquote_env_value(v.strip())
    for key, original in TRICKY_VALUES.items():
        assert recovered[key] == original, f"{key}: wrote {original!r}, Python reader got {recovered[key]!r}"


def test_write_env_rejects_embedded_newline(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_copy = scripts_dir / "write-env.sh"
    script_copy.write_bytes(WRITE_ENV_SCRIPT.read_bytes())
    script_copy.chmod(0o755)
    content = env_store._encode_env_pairs({"A": "line one\nline two"})
    proc = subprocess.run(["bash", str(script_copy)], input=content, capture_output=True, timeout=15, cwd=str(scripts_dir))
    assert proc.returncode != 0
    assert not (tmp_path / ".env").exists()
