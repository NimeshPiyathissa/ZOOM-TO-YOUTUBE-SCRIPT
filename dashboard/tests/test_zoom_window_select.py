"""scripts/zoom-meeting-only.sh picks the MEETING window (Step A.1).

Production 2026-11-08: Zoom 7.2.1 had its home window "Zoom Workplace - Free account" and the meeting window "Webinar"
open, both 1920x1080 and the home window listed first. The old rule (title /Zoom (Meeting|Webinar)/, else the largest)
picked the home window; fullscreening it raised it over the webinar and Clean Broadcast measured Zoom's home screen as
"clean". Fake wmctrl/xprop/xdotool replay real listings and record what the script does to which window."""
import os
import pathlib
import shutil
import subprocess

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash required")
SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "zoom-meeting-only.sh"

HOME = "0x0020000d  0 0    0    1920 1080 zoom.zoom             ip-172-31-19-223 Zoom Workplace - Free account"
WEBINAR = "0x0020002b  0 0    0    1920 1080 zoom.zoom             ip-172-31-19-223 Webinar"
BARS = ["0x0020002e  0 690  452  564  78   zoom.zoom             ip-172-31-19-223 zoom",
        "0x0020005b  0 579  496  761  78   zoom.zoom             ip-172-31-19-223 zoom"]
CHROME = "0x01400003  0 0    0    1920 1080 google-chrome.Google-chrome ip-172-31-19-223 YouTube - Google Chrome"


def _posix(p):
    p = pathlib.Path(p).resolve().as_posix()
    return "/" + p[0].lower() + p[2:] if len(p) > 1 and p[1] == ":" else p


def run(tmp_path, windows, stack, action, fullscreen=()):
    """windows: wmctrl -lGx lines; stack: ids bottom->top as xprop prints them. Returns (stdout, rc, calls, fs)."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (tmp_path / "windows").write_text("\n".join(windows) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "fs").write_text("\n".join(fullscreen) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "calls").write_text("", encoding="utf-8")
    d = _posix(tmp_path)
    fakes = {
        "wmctrl": f'''#!/usr/bin/env bash
echo "wmctrl $*" >> {d}/calls
case "$1" in
  -lGx) cat {d}/windows ;;
  -lG) awk '{{ printf "%s %s %s %s %s %s %s", $1, $2, $3, $4, $5, $6, $8; for (i = 9; i <= NF; i++) printf " %s", $i; print "" }}' {d}/windows ;;
  -i) if [[ "$2" == -r && "$4" == -b && "$5" == add,fullscreen ]]; then echo "$3" >> {d}/fs; fi
      if [[ "$2" == -r && "$4" == -b && "$5" == remove,fullscreen ]]; then grep -v "^$3$" {d}/fs > {d}/fs.n; mv {d}/fs.n {d}/fs; fi ;;
esac
''',
        "xprop": f'''#!/usr/bin/env bash
if [[ "$1" == -root ]]; then echo "_NET_CLIENT_LIST_STACKING(WINDOW): window id # {", ".join(stack)}"; exit 0; fi
if [[ "$1" == -id ]]; then grep -qx "$2" {d}/fs && echo "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN" || echo "_NET_WM_STATE(ATOM) = "; fi
''',
        "xdotool": f'''#!/usr/bin/env bash
echo "xdotool $*" >> {d}/calls
[[ "$1" == getdisplaygeometry ]] && echo "1920 1080"
exit 0
''',
    }
    for name, body in fakes.items():
        (b / name).write_text(body, encoding="utf-8", newline="\n")
        os.chmod(b / name, 0o755)
    env = dict(os.environ, PATH=_posix(b) + os.pathsep + os.environ.get("PATH", ""), DISPLAY=":99")
    r = subprocess.run([BASH, _posix(SCRIPT), action], capture_output=True, text=True, env=env, timeout=30)
    calls = (tmp_path / "calls").read_text().splitlines()
    fs = [x for x in (tmp_path / "fs").read_text().split() if x]
    return r.stdout + r.stderr, r.returncode, calls, fs


def geometry(out):
    line = out.strip().splitlines()[-1]
    return dict(p.split("=", 1) for p in line.split() if "=" in p)


def test_production_case_fullscreens_and_raises_the_webinar_not_the_home_window(tmp_path):
    out, rc, calls, fs = run(tmp_path, [HOME, WEBINAR, *BARS], ["0x20000d", "0x20002b"], "on")
    assert rc == 0 and fs == ["0x0020002b"], out
    assert "wmctrl -i -r 0x0020002b -b add,fullscreen" in calls and "wmctrl -i -a 0x0020002b" in calls
    assert not any("0x0020000d" in c for c in calls if "-b" in c or " -a " in c)
    assert "meeting window raised" in out and any(c.startswith("xdotool mousemove 2 ") for c in calls)


def test_geometry_reports_the_meeting_and_whether_it_is_on_top(tmp_path):
    out, rc, _, _ = run(tmp_path, [HOME, WEBINAR], ["0x20000d", "0x20002b"], "geometry", fullscreen=["0x0020002b"])
    g = geometry(out)
    assert rc == 0 and g["w"] == "1920" and g["fullscreen"] == "1" and g["meeting"] == "1" and g["top"] == "1"
    out, _, calls, _ = run(tmp_path, [HOME, WEBINAR], ["0x20002b", "0x20000d"], "geometry")      # home raised over it
    g = geometry(out)
    assert g["meeting"] == "1" and g["top"] == "0" and g["fullscreen"] == "0"
    assert not any(" -r " in c or " -a " in c for c in calls)                                   # read-only


@pytest.mark.parametrize("title", ["Zoom Meeting", "Zoom Webinar", "Meeting", "Webinar", "Physics revision 2027", "Zoom Workplace"])
def test_meeting_titles_win_over_the_home_window_whatever_the_order(tmp_path, title):
    meet = f"0x00200099  0 0    0    1920 1080 zoom.zoom             ip-172-31-19-223 {title}"
    for order in ([HOME, meet], [meet, HOME]):
        out, rc, _, fs = run(tmp_path, order, ["0x20000d", "0x200099"], "on")
        assert rc == 0 and fs == ["0x00200099"], (title, order, out)


def test_pop_out_panels_and_other_apps_are_never_picked(tmp_path):
    settings = "0x00200077  0 300  100  1300 900  zoom.zoom             ip-172-31-19-223 Settings"
    chat = "0x00200078  0 0    0    1920 1080 zoom.zoom             ip-172-31-19-223 Chat"
    out, rc, _, fs = run(tmp_path, [HOME, settings, chat, CHROME, WEBINAR], ["0x20000d", "0x200077", "0x200078", "0x20002b"], "on")
    assert rc == 0 and fs == ["0x0020002b"], out


def test_explicit_meeting_title_beats_a_topic_titled_window(tmp_path):
    topic = "0x00200055  0 0    0    1920 1080 zoom.zoom             ip-172-31-19-223 Class notes"
    _, _, _, fs = run(tmp_path, [topic, WEBINAR, HOME], ["0x20002b", "0x200055", "0x20000d"], "on")
    assert fs == ["0x0020002b"]


def test_only_the_home_window_keeps_the_old_behaviour_but_says_it_is_not_a_meeting(tmp_path):
    out, rc, calls, fs = run(tmp_path, [HOME, *BARS], ["0x20000d"], "on")
    assert rc == 0 and fs == ["0x0020000d"] and "meeting window raised" not in out
    assert not any(" -a " in c for c in calls)
    g = geometry(run(tmp_path, [HOME], ["0x20000d"], "geometry")[0])
    assert g["meeting"] == "0" and g["top"] == "1"


def test_no_zoom_window_fails_with_a_message(tmp_path):
    out, rc, calls, fs = run(tmp_path, [CHROME, *BARS], ["0x1400003"], "on")
    assert rc == 1 and "not found" in out and fs == [] and not any(" -r " in c for c in calls)


def test_off_and_idempotent_on(tmp_path):
    out, rc, calls, fs = run(tmp_path, [HOME, WEBINAR], ["0x20000d", "0x20002b"], "on", fullscreen=["0x0020002b"])
    assert rc == 0 and "already fullscreen" in out and "wmctrl -i -a 0x0020002b" in calls
    out, rc, _, fs = run(tmp_path, [HOME, WEBINAR], ["0x20000d", "0x20002b"], "off", fullscreen=["0x0020002b", "0x0020000d"])
    assert rc == 0 and fs == ["0x0020000d"]                                    # only the meeting window was changed
