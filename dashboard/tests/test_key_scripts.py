"""Stream-key hygiene scripts (prepared for review, never run on production by the tests): the log scrubber against
throwaway fake keys, and static checks that the /proc hardening script is report-only by default."""
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRUB = REPO / "scripts" / "scrub-ffmpeg-log.sh"
HARDEN = REPO / "scripts" / "harden-proc-visibility.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")
KEY = "fake-key-1234-abcd-5678"


def _posix(p):
    p = pathlib.Path(p).resolve().as_posix()
    return "/" + p[0].lower() + p[2:] if len(p) > 1 and p[1] == ":" else p


def scrub(*args):
    r = subprocess.run([BASH, _posix(SCRUB), *args], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def logs(tmp_path):
    line = f"Output #0, flv, to 'rtmps://a.rtmps.youtube.com:443/live2/{KEY}':\n"
    (tmp_path / "ffmpeg.log").write_text(line + "frame=1 fps=30 speed=1x\nrtmps://a.rtmps.youtube.com/live2/<redacted>\n")
    (tmp_path / "ffmpeg.log.1").write_text(line)
    return tmp_path


def test_dry_run_changes_nothing_and_never_prints_the_key(logs):
    before = {p.name: p.read_bytes() for p in logs.iterdir()}
    rc, out = scrub("--dir", _posix(logs))
    assert rc == 1 and "WOULD SCRUB" in out and "DRY RUN" in out
    assert KEY not in out and "live2/" not in out                       # counts and file names only
    assert {p.name: p.read_bytes() for p in logs.iterdir()} == before


def test_apply_masks_every_key_keeps_everything_else_and_leaves_no_temp_or_backup(logs):
    rc, out = scrub("--apply", "--dir", _posix(logs))
    assert rc == 0 and KEY not in out
    for p in logs.iterdir():
        text = p.read_text()
        assert KEY not in text
    main = (logs / "ffmpeg.log").read_text()
    assert "rtmps://a.rtmps.youtube.com:443/live2/<redacted>" in main and "frame=1 fps=30 speed=1x" in main
    assert sorted(p.name for p in logs.iterdir()) == ["ffmpeg.log", "ffmpeg.log.1"]       # no .scrub.* temp, no backup copy
    rc, out = scrub("--dir", _posix(logs))
    assert rc == 0 and "clean" in out                                                    # idempotent


def test_scrub_rejects_unknown_option_and_missing_dir(tmp_path):
    assert scrub("--nope")[0] == 2
    assert scrub("--dir", _posix(tmp_path / "missing"))[0] == 2


def test_hardening_script_parses_and_is_report_only_by_default():
    assert subprocess.run([BASH, "-n", _posix(HARDEN)], capture_output=True).returncode == 0
    text = HARDEN.read_text()
    assert "MODE=report" in text
    head, _, tail = text.partition('if [ "$MODE" = report ]; then')
    assert "exit 0" in tail.split("fi", 1)[0]                          # report mode exits before anything below
    for cmd in ("mount -o", "groupadd", "usermod", ">>/etc/fstab", "sed -i"):
        assert cmd not in head, f"{cmd!r} appears before the report-only exit"
    assert re.search(r"must run as root", tail)
    assert "ffmpeg" not in re.sub(r"#.*", "", text).replace("ffmpeg's", "")      # never touches the encoder
