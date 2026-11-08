"""Remote GUI (/vnc) view modes: the page markup and the geometry module.

The geometry itself (static/js/vnc-fit.js) is tested in tests/js/vnc-fit.test.mjs with node's built-in
runner; this file runs it under pytest when node is installed, so CI and local runs see one result."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import app
from app import db, config, security

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_dashboard.db")
    db.init_db()
    with db.get_conn() as conn:
        user_id = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("admin", security.hash_password("correct-horse-battery-staple"), time.time()),
        ).lastrowid
    session_id, _csrf = security.create_session(user_id, "127.0.0.1")
    return TestClient(app, cookies={config.COOKIE_NAME: session_id})


def test_vnc_page_has_view_modes_fit_window_and_reset(client):
    html = client.get("/vnc").text
    for mode in ("smart", "fit", "fill", "native"):
        assert f'data-view="{mode}"' in html
    assert 'id="vnc-btn-scale"' in html and "Fit Window" in html
    assert 'id="vnc-btn-reset"' in html
    assert 'id="vnc-frame"' in html            # noVNC renders into the frame, not the stage
    assert 'id="vnc-fit-text"' in html          # visible fit feedback
    assert 'data-view="smart" aria-pressed="true"' in html   # Smart Fit is the default


def test_vnc_page_never_touches_the_broadcast():
    # The view code only sizes boxes; it must not call stream/broadcast endpoints.
    for name in ("vnc.js", "vnc-fit.js"):
        src = (ROOT / "static" / "js" / name).read_text(encoding="utf-8")
        for forbidden in ("/api/stream", "/api/broadcast", "go_live", "ffmpeg-stream"):
            assert forbidden not in src, f"{name} references {forbidden}"


def test_vnc_fit_geometry_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    res = subprocess.run([node, "--test", str(ROOT / "tests" / "js" / "vnc-fit.test.mjs")],
                         capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
