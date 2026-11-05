"""cdp.py regression tests, run against a fake Chrome DevTools server (real HTTP + websocket on loopback).

Covers the performance change (the loopback HTTP client skips TLS setup: httpx builds a CA-bundle TLS context
per client unless verify=False, ~500 ms CPU per DevTools call on a small box) and proves it did not change
behaviour for the three callers that depend on cdp: Zoom web (clean feed / status), YouTube (watcher state) and
EduMix (status / navigate / fit)."""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from websockets.sync.server import serve

from app import cdp, config, edumix, youtube_watch, zoom_web


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeChrome:
    """HTTP /json* + a websocket per page target, like Chrome's remote-debugging port.
    `answer(expression)` returns the value Runtime.evaluate should produce (or raises to simulate a JS exception)."""

    def __init__(self):
        self.http_port, self.ws_port = _free_port(), _free_port()
        self.targets = [("page", "https://mjphysics.lk/dashboard", "EduMix")]
        self.requests: list[dict] = []     # HTTP request headers
        self.messages: list[dict] = []     # CDP messages received over the websocket
        self.answer = lambda expr: None

    def _target_json(self):
        out = []
        for i, (typ, url, title) in enumerate(self.targets):
            out.append({"id": f"T{i}", "type": typ, "url": url, "title": title,
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.ws_port}/devtools/page/T{i}"})
        return out

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                body = {"/json": outer._target_json(), "/json/version": {"Browser": "FakeChrome/1"}}.get(self.path)
                if body is None:
                    self.send_response(404); self.end_headers(); return
                raw = json.dumps(body).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def log_message(self, *a):
                pass

        def handler(conn):
            for raw in conn:
                msg = json.loads(raw)
                outer.messages.append(msg)
                if msg["method"] == "Runtime.evaluate":
                    try:
                        val = outer.answer(msg["params"]["expression"])
                        res = {"result": {"type": "object", "value": val}}
                    except RuntimeError as exc:
                        res = {"result": {"type": "object", "subtype": "error", "description": str(exc)},
                               "exceptionDetails": {"text": "Uncaught", "exception": {"description": str(exc)}}}
                else:
                    res = {}
                conn.send(json.dumps({"id": msg["id"], "result": res}))

        self.http = HTTPServer(("127.0.0.1", self.http_port), H)
        self.ws = serve(handler, "127.0.0.1", self.ws_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.http.shutdown(); self.http.server_close(); self.ws.shutdown()


@pytest.fixture
def chrome(monkeypatch):
    c = FakeChrome().start()
    monkeypatch.setattr(config, "CHROME_DEBUG_PORT", c.http_port)
    yield c
    c.stop()


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    edumix.invalidate()
    yield
    edumix.invalidate()


# ------------------------------------------------------------------ the optimization itself

def test_devtools_http_client_skips_tls_setup_and_is_loopback_http_only(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return httpx.Response(200, json=[{"type": "page"}], request=httpx.Request("GET", url))

    monkeypatch.setattr(cdp.httpx, "AsyncClient", FakeClient)
    assert run(cdp._get_json("")) == [{"type": "page"}]
    assert seen["verify"] is False
    assert seen["url"] == f"http://127.0.0.1:{config.CHROME_DEBUG_PORT}/json"


@pytest.mark.parametrize("path", ["", "/version"])
def test_devtools_url_is_always_plain_http_loopback(path):
    url = cdp._devtools_url(path)
    assert url.startswith("http://127.0.0.1:") and "https" not in url and url.endswith("/json" + path)


def test_non_loopback_or_https_url_is_refused_before_any_client_exists(monkeypatch):
    """Even if a bad value reached the URL builder, verify=False could not apply: no client is created."""
    created = []
    monkeypatch.setattr(cdp.httpx, "AsyncClient", lambda **kw: created.append(kw))
    monkeypatch.setattr(cdp, "urlsplit", lambda u: type("P", (), {"scheme": "https", "hostname": "evil.example"})())
    with pytest.raises(cdp.CDPError, match="non-loopback"):
        run(cdp._get_json(""))
    assert created == []


def test_exactly_one_http_client_and_it_is_the_loopback_one():
    """AST check: cdp.py builds a single httpx.AsyncClient (in _get_json) with verify=False, and has no custom
    ssl handling - so TLS verification is never switched off for anything else in the module."""
    import ast
    tree = ast.parse(open(cdp.__file__, encoding="utf-8").read())
    clients = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "AsyncClient"]
    assert len(clients) == 1
    assert {k.arg: getattr(k.value, "value", None) for k in clients[0].keywords}.get("verify") is False
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_get_json")
    assert clients[0] in list(ast.walk(fn))                       # ...and it lives inside _get_json
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "ssl" not in imports and not any(isinstance(n, ast.Attribute) and n.attr in ("SSLContext", "create_default_context")
                                            for n in ast.walk(tree))


def test_client_is_closed_on_success_and_on_every_failed_attempt(monkeypatch):
    log = {"enter": 0, "exit": 0}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            log["enter"] += 1
            return self

        async def __aexit__(self, *a):
            log["exit"] += 1
            return False

        async def get(self, url):
            raise httpx.ConnectError("refused")

    async def no_sleep(_):
        pass
    monkeypatch.setattr(cdp.httpx, "AsyncClient", Client)
    monkeypatch.setattr(cdp.asyncio, "sleep", no_sleep)
    with pytest.raises(cdp.CDPError, match="DevTools port"):
        run(cdp._get_json(""))
    assert log == {"enter": 2, "exit": 2}                          # one quick retry, both clients closed


def test_unreachable_port_still_raises_cdperror_after_one_retry(monkeypatch):
    monkeypatch.setattr(config, "CHROME_DEBUG_PORT", _free_port())  # nothing listening
    calls = []
    real = cdp.httpx.AsyncClient

    def counting(**kw):
        calls.append(kw)
        return real(**kw)
    monkeypatch.setattr(cdp.httpx, "AsyncClient", counting)
    with pytest.raises(cdp.CDPError, match="isn't reachable"):
        run(cdp.probe())
    assert len(calls) == 2


# ------------------------------------------------------------------ real protocol, behaviour unchanged

def test_probe_navigate_evaluate_over_real_http_and_websocket(chrome):
    info = run(cdp.probe())
    assert info["connected"] and info["browser"] == "FakeChrome/1" and info["pages"] == 1 and info["url"].endswith("/dashboard")
    run(cdp.navigate("https://example.com/x"))
    assert chrome.messages[-1]["method"] == "Page.navigate" and chrome.messages[-1]["params"] == {"url": "https://example.com/x"}
    chrome.answer = lambda expr: {"echo": expr}
    res = run(cdp.evaluate("1+1", user_gesture=True))
    assert res["value"] == {"echo": "1+1"}
    ev = chrome.messages[-1]
    assert ev["method"] == "Runtime.evaluate" and ev["params"]["userGesture"] is True and ev["params"]["returnByValue"] is True


def test_js_exceptions_still_surface_as_cdperror(chrome):
    def boom(expr):
        raise RuntimeError("ReferenceError: nope")
    chrome.answer = boom
    with pytest.raises(cdp.CDPError, match="nope"):
        run(cdp.evaluate("nope()"))


def test_no_credentials_or_auth_headers_are_sent(chrome):
    run(cdp.probe()); run(cdp.evaluate("1"))
    assert chrome.requests
    for r in chrome.requests:
        hdrs = {k.lower() for k in r["headers"]}
        assert not hdrs & {"authorization", "cookie", "proxy-authorization"}
        assert r["path"].startswith("/json")


def test_every_http_request_goes_to_loopback_http(chrome, monkeypatch):
    urls = []
    real_get = httpx.AsyncClient.get

    async def spy(self, url, *a, **k):
        urls.append(str(url))
        return await real_get(self, url, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "get", spy)
    chrome.answer = lambda e: {}
    run(cdp.probe()); run(cdp.navigate("about:blank")); run(cdp.evaluate("1"))
    assert urls and all(u.startswith("http://127.0.0.1:") for u in urls)


def test_zoom_page_is_still_preferred_among_several_tabs(chrome):
    chrome.targets = [("page", "https://example.com/a", "other"), ("page", "https://us06web.zoom.us/wc/join/123", "Zoom"),
                      ("service_worker", "https://x/sw.js", "sw")]
    run(cdp.navigate("about:blank"))
    assert chrome.messages  # connected to a page target without error
    assert run(cdp._get_page_target())["url"].startswith("https://us06web.zoom.us")


def test_no_page_target_raises(chrome):
    chrome.targets = [("service_worker", "https://x/sw.js", "sw")]
    with pytest.raises(cdp.CDPError, match="No Chrome page target"):
        run(cdp.evaluate("1"))


# ------------------------------------------------------------------ the three callers, end to end

def test_zoom_web_status_and_cleanfeed_through_cdp(chrome):
    chrome.targets = [("page", "https://us06web.zoom.us/wc/join/123", "Zoom")]
    chrome.answer = lambda expr: ({"url": "https://us06web.zoom.us/wc/123/start", "on_zoom": True, "body_text": "", "leave_present": True, "footer_present": True, "labels": [], "playing_video": True}
                                  if expr == zoom_web.JS_STATE else {"removed": 3})
    st = run(zoom_web._status_async())
    assert st["status"] == "in_meeting"
    cf = run(zoom_web.apply_cleanfeed_async())
    assert cf == {"ok": True, "result": {"removed": 3}}
    chrome.stop()                                              # Chrome gone: callers degrade, not crash
    chrome.http_port = _free_port()
    assert run(zoom_web._status_async())["status"] == "unknown"


def test_youtube_watcher_state_through_cdp(chrome):
    chrome.targets = [("page", "https://www.youtube.com/watch?v=abc", "YouTube")]
    chrome.answer = lambda expr: {"ready_state": 4, "paused": False, "url": "https://www.youtube.com/watch?v=abc"}
    st = run(youtube_watch.read_state())
    assert st["available"] is True and st["ready_state"] == 4
    assert chrome.messages[-1]["params"]["expression"] == cdp.JS_STATE
    run(cdp.evaluate(cdp.js_set_volume(40)))
    assert "volume" in chrome.messages[-1]["params"]["expression"]


def test_youtube_watcher_reports_unavailable_when_chrome_is_down(monkeypatch):
    monkeypatch.setattr(config, "CHROME_DEBUG_PORT", _free_port())
    st = run(youtube_watch.read_state())
    assert st["available"] is False and "DevTools" in st["reason"]


def test_edumix_status_navigate_and_fit_through_cdp(chrome):
    page = {"url": "https://mjphysics.lk/class/1", "ready": "complete", "online": True, "login_form": False, "otp_form": False,
            "inputs": 0, "markers": {"logout": True}, "frames": [],
            "videos": [{"paused": False, "ended": False, "readyState": 4, "networkState": 1, "error": 0, "time": 3,
                        "w": 1280, "h": 720, "protected": False, "muted": False, "volume": 1}]}

    def answer(expr):
        return json.dumps(page) if expr == edumix.JS_PROBE else (1 if "objectFit" in expr else None)
    chrome.answer = answer
    st = run(edumix.status())
    assert st["state"] == "playing" and st["capture"]["enabled"] is False
    run(edumix.navigate_class("https://mjphysics.lk/class/2?x=1"))
    assert chrome.messages[-1] == {"id": 1, "method": "Page.navigate", "params": {"url": "https://mjphysics.lk/class/2?x=1"}}
    assert run(edumix.set_video_fit("fill"))["applied"] is True
    page["videos"][0]["protected"] = True
    edumix.invalidate()
    st = run(edumix.status())
    assert st["state"] == "protected" and st["capture"]["enabled"] is False


def test_edumix_capture_stays_disabled_and_production_settings_untouched():
    """The optimization must not have moved the EduMix capture gate or any streaming setting."""
    assert config.EDUMIX_CAPTURE_ENABLED is False
    assert edumix.capture_blocked("edumix") and not edumix.capture_blocked("zoom")
    src = open(cdp.__file__, encoding="utf-8").read()
    for forbidden in ("EDUMIX_CAPTURE_ENABLED", "STREAM_", "YT_STREAM_KEY", "ffmpeg"):
        assert forbidden not in src
