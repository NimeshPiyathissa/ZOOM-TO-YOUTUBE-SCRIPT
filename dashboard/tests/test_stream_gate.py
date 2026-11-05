"""scripts/stream.sh + lib.sh: the one-time start permission and the stream-key-safe logging. These run the REAL shell
code (bash required) against a sandbox APP_DIR with a fake `ffmpeg`; nothing touches a network or a real encoder.
The fake ffmpeg prints the same startup line the real one does (it names the full RTMP destination) so redaction is
tested against realistic output."""
import os
import pathlib
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

KEY = "abcd-efgh-ijkl-mnop-qrst"          # looks like a real key, is not one


def _posix(p):
    p = pathlib.Path(p).resolve().as_posix()
    if len(p) > 1 and p[1] == ":":                       # C:/x -> /c/x for git-bash on Windows
        p = "/" + p[0].lower() + p[2:]
    return p


@pytest.fixture
def sandbox(tmp_path):
    app = tmp_path / "zs"
    (app / "scripts").mkdir(parents=True)
    for n in ("lib.sh", "stream.sh"):
        text = (SCRIPTS / n).read_text(encoding="utf-8").replace("\r\n", "\n")
        (app / "scripts" / n).write_text(text.replace("/usr/bin/ffmpeg", _posix(app / "fake-ffmpeg")), encoding="utf-8", newline="\n")
    (app / ".env").write_text(f'YT_STREAM_KEY="{KEY}"\nVNC_PASSWORD="secretvnc"\nRESOLUTION="1280x720"\nFPS="30"\n', encoding="utf-8")
    (app / "fake-ffmpeg").write_text(
        "#!/usr/bin/env bash\n"
        f"echo invoked >> '{_posix(app)}/ffmpeg-ran'\n"
        f"env > '{_posix(app)}/ffmpeg-env'\n"
        f"printf '%s\\n' \"$*\" > '{_posix(app)}/ffmpeg-argv'\n"
        "echo \"Output #0, flv, to '${@: -1}':\" >&2\n"
        "echo 'frame=  100 fps= 30 q=20.0 size=1kB time=00:00:03 bitrate=6000kbits/s speed=1x' >&2\n"
        "exit 0\n", encoding="utf-8", newline="\n")
    os.chmod(app / "fake-ffmpeg", 0o755)
    return app


def write_source(app, nonce="", expires=0):
    (app / "current-source.env").write_text(
        'SOURCE_TYPE="direct"\nDIRECT_URL="https://example.com/live.m3u8"\nDIRECT_MODE="reencode"\n'
        f'BROADCAST_AUTH_ID="{"gid" if nonce else ""}"\nBROADCAST_AUTH_NONCE="{nonce}"\nBROADCAST_AUTH_EXPIRES="{expires if nonce else ""}"\n',
        encoding="utf-8", newline="\n")


def run_stream(app):
    env = dict(os.environ, APP_DIR=_posix(app), HOME=_posix(app))
    env.pop("DISPLAY", None)
    return subprocess.run([BASH, _posix(app / "scripts" / "stream.sh")], capture_output=True, text=True, env=env, timeout=60)


def source_env(app):
    return (app / "current-source.env").read_text()


def test_without_authorization_the_encoder_is_never_started_and_it_is_not_a_failure(sandbox):
    write_source(sandbox)
    r = run_stream(sandbox)
    assert r.returncode == 0                                       # exit 0: systemd's Restart=on-failure must not loop on it
    assert not (sandbox / "ffmpeg-ran").exists()
    assert "no valid one-time broadcast authorization" in r.stdout


def test_an_expired_permission_is_refused(sandbox):
    write_source(sandbox, "n0nce", int(time.time()) - 5)
    assert run_stream(sandbox).returncode == 0 and not (sandbox / "ffmpeg-ran").exists()


def test_a_valid_permission_starts_once_and_is_burned(sandbox):
    write_source(sandbox, "n0nce", int(time.time()) + 30)
    r = run_stream(sandbox)
    assert r.returncode == 0 and (sandbox / "ffmpeg-ran").exists()
    assert 'BROADCAST_AUTH_NONCE=""' in source_env(sandbox) and "n0nce" not in source_env(sandbox)      # burned on disk
    (sandbox / "ffmpeg-ran").unlink()
    # a crash restart by systemd, a reboot, a second start: the same file no longer authorizes anything
    r2 = run_stream(sandbox)
    assert r2.returncode == 0 and not (sandbox / "ffmpeg-ran").exists()


def test_a_stale_nonce_in_the_environment_that_is_not_on_disk_is_refused(sandbox):
    write_source(sandbox)                                           # disk says: no permission
    env = dict(os.environ, APP_DIR=_posix(sandbox), HOME=_posix(sandbox), BROADCAST_AUTH_NONCE="n0nce",
               BROADCAST_AUTH_EXPIRES=str(int(time.time()) + 30))
    r = subprocess.run([BASH, _posix(sandbox / "scripts" / "stream.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and not (sandbox / "ffmpeg-ran").exists()


def test_two_simultaneous_starts_with_one_permission_start_exactly_one_encoder(sandbox):
    write_source(sandbox, "n0nce", int(time.time()) + 30)
    env = dict(os.environ, APP_DIR=_posix(sandbox), HOME=_posix(sandbox))
    env.pop("DISPLAY", None)
    cmd = [BASH, _posix(sandbox / "scripts" / "stream.sh")]
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env) for _ in range(4)]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs)
    ran = (sandbox / "ffmpeg-ran").read_text().split() if (sandbox / "ffmpeg-ran").exists() else []
    assert len(ran) == 1, outs


def test_the_stream_key_never_reaches_the_log_or_the_environment(sandbox):
    write_source(sandbox, "n0nce", int(time.time()) + 30)
    r = run_stream(sandbox)
    assert r.returncode == 0
    log = (sandbox / "logs" / "ffmpeg.log").read_text()
    assert KEY not in log and "live2/<redacted>" in log                                   # masked, destination still identifiable
    assert "frame=  100 fps= 30" in log                                                  # progress lines still flow
    assert KEY not in r.stdout and KEY not in r.stderr
    env_dump = (sandbox / "ffmpeg-env").read_text()
    assert KEY not in env_dump and "secretvnc" not in env_dump                            # not inherited by the encoder
    assert KEY in (sandbox / "ffmpeg-argv").read_text()                                   # (the URL itself still has to reach ffmpeg)


# ------------------------------------------------------------------ the redaction filter on its own

def redact(text):
    script = f'source "{_posix(SCRIPTS / "lib.sh")}"; redact_stream_secrets'
    env = dict(os.environ, APP_DIR=_posix(pathlib.Path.cwd()), ZOOMBOT_RUNTIME_ENV_FILE=_posix(_env_file()))
    return subprocess.run([BASH, "-c", script], input=text, capture_output=True, text=True, env=env, timeout=30).stdout


_ENV = None


def _env_file():
    global _ENV
    if _ENV is None:
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "e.env").write_text("X=1\n")
        _ENV = d / "e.env"
    return _ENV


@pytest.mark.parametrize("line", [
    f"Output #0, flv, to 'rtmps://a.rtmps.youtube.com:443/live2/{KEY}':",
    f"[flv @ 0x1] Failed to connect rtmps://a.rtmps.youtube.com:443/live2/{KEY} (Input/output error)",
    f"rtmp://a.rtmp.youtube.com/live2/{KEY}?backup=1",
    f'Opening "rtmps://a.rtmps.youtube.com:443/live2/{KEY}" for writing',
])
def test_destination_urls_are_masked_in_every_common_shape(line):
    out = redact(line + "\n")
    assert KEY not in out and "<redacted>" in out


def test_other_lines_pass_through_untouched():
    text = "frame= 100 fps= 30 q=20.0 bitrate=6000kbits/s\nStream mapping:\n  Stream #0:0 -> #0:0 (rawvideo -> h264)\n"
    assert redact(text) == text
