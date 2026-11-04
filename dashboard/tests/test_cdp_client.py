"""cdp._get_json talks plain http to Chrome's loopback DevTools port. It must not pay for a TLS context
(httpx loads the CA bundle per client unless verify=False: ~500 ms CPU per call on a small machine)."""
import asyncio

import httpx

from app import cdp


def test_devtools_http_client_skips_tls_setup(monkeypatch):
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
    out = asyncio.new_event_loop().run_until_complete(cdp._get_json(""))
    assert out == [{"type": "page"}]
    assert seen["verify"] is False and seen["url"].startswith("http://127.0.0.1:")
