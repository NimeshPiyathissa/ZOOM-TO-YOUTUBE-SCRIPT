"""Static guards for 'only an explicit Go Live starts FFmpeg': the unit file cannot self-restart or autostart, no scheduler or
watchdog code asks for an encoder start, and every Python call that starts the encoder passes a grant."""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
APP = REPO / "dashboard" / "app"


def test_encoder_unit_has_no_restart_and_no_install_section():
    text = (REPO / "systemd" / "ffmpeg-stream.service").read_text(encoding="utf-8")
    live = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert "Restart=no" in live and not any(ln.startswith("Restart=") and ln != "Restart=no" for ln in live)
    assert "[Install]" not in live and not any(ln.startswith("WantedBy=") for ln in live)


def test_scheduler_and_watchdog_never_start_or_restart_the_encoder():
    src = (APP / "scheduler.py").read_text(encoding="utf-8")
    assert not re.search(r"unit_action\(\s*[\"']ffmpeg-stream[\"']\s*,\s*[\"'](start|restart)", src)
    assert "start_source(" not in src or "encoder_grant" not in src


def test_every_encoder_start_in_python_goes_through_a_grant():
    for path in APP.glob("*.py"):
        if path.name in ("localdev.py", "broadcast.py"):          # broadcast.py only mentions the call in prose
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"unit_action\([^)]*[\"']ffmpeg-stream[\"'][^)]*[\"'](start|restart)[\"']", line) and "grant" not in line:
                raise AssertionError(f"{path.name}:{n} starts the encoder without a grant: {line.strip()}")


def test_manage_sh_go_live_refuses_and_nothing_enables_the_encoder_unit():
    manage = (REPO / "scripts" / "manage.sh").read_text(encoding="utf-8")
    assert "go-live" in manage and "systemctl --user start ffmpeg-stream" not in manage
    for path in (REPO / "scripts").glob("*.sh"):
        assert not re.search(r"systemctl[^\n]*\benable\b[^\n]*ffmpeg-stream", path.read_text(encoding="utf-8")), path.name
