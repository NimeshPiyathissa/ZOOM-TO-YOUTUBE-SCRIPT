#!/usr/bin/env python3
"""Hold one or more Remote GUI pages open in a headless browser for a
given duration and report every WebSocket close seen on each `/vnc/ws`
connection, plus any time the page's own status badge drops out of
"connected".

Monitoring here deliberately uses Playwright's *native* page.on("websocket")
event (backed by CDP Network domain observation) rather than monkey-patching
window.WebSocket from an injected script. An earlier version of this script
did the latter and reliably produced a fake ~10.4s failure, every run,
headless or headful, over plain HTTP or HTTPS - a classic observer-effect
bug in the patch itself (overriding the global WebSocket constructor
changed real behaviour), not a real server/client issue: a raw concurrent
WebSocket burst against the same server with no browser involved held
cleanly, and removing the monkey-patch (watching only via Playwright's
native, non-invasive hook, or purely from vnc.js's own internal state)
made the failure disappear completely and reproducibly. Lesson kept here
so nobody reintroduces that pattern chasing a phantom bug.

Needs a real deployed dashboard + login - see README.md in this directory.

Usage:
    ZSDASH_URL=https://your-dashboard ZSDASH_USER=... ZSDASH_PASS=... \
        python vnc_hold_open.py --pages vnc --minutes 30
    python vnc_hold_open.py --pages vnc,remote --minutes 30
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright

BADGE_SELECTOR = {
    "vnc": "#vnc-badge-text",
    "remote": "#rd-status",
}


async def login(context, base_url: str, username: str, password: str) -> None:
    page = await context.new_page()
    await page.goto(f"{base_url}/login")
    await page.fill('input[name="username"]', username)
    await page.fill('input[name="password"]', password)
    await page.click('button[type="submit"], input[type="submit"]')
    await page.wait_for_load_state("networkidle")
    await page.close()


class Watcher:
    """Tracks /vnc/ws WebSocket closes for one page via Playwright's
    native (non-invasive) websocket event, plus badge-text snapshots."""

    def __init__(self, page, name: str):
        self.page = page
        self.name = name
        self.ws_count = 0
        self.close_events: list[dict] = []
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, ws) -> None:
        if "/vnc/ws" not in ws.url:
            return
        self.ws_count += 1
        opened_at = time.monotonic()
        ws.on("close", lambda: self.close_events.append({
            "seconds_open": round(time.monotonic() - opened_at, 1),
        }))
        ws.on("socketerror", lambda err: self.close_events.append({
            "seconds_open": round(time.monotonic() - opened_at, 1), "error": str(err),
        }))

    async def badge_text(self) -> str:
        sel = BADGE_SELECTOR.get(self.name)
        if not sel:
            return ""
        loc = self.page.locator(sel)
        try:
            if await loc.count():
                return (await loc.text_content()) or ""
        except Exception:
            pass
        return ""


async def open_vnc(context, base_url: str) -> Watcher:
    page = await context.new_page()
    watcher = Watcher(page, "vnc")
    await page.goto(f"{base_url}/vnc")
    try:
        connect_btn = page.locator("#vnc-overlay-btn")
        if await connect_btn.is_visible(timeout=3000):
            await connect_btn.click()
    except Exception:
        pass
    return watcher


async def open_remote(context, base_url: str) -> Watcher:
    page = await context.new_page()
    watcher = Watcher(page, "remote")
    await page.goto(f"{base_url}/remote")
    toggle = page.locator("#rd-toggle")
    status = page.locator("#rd-status")
    try:
        await toggle.wait_for(timeout=5000)
        for _ in range(3):
            label = await status.text_content() if await status.count() else ""
            if label and "Interactive" in label:
                break
            await toggle.click()
            await asyncio.sleep(2)
    except Exception:
        pass
    return watcher


async def open_accounts(context, base_url: str) -> Watcher:
    # Deliberately does NOT click "Sign in" on any account - that launches
    # a real Google sign-in flow on the shared Chrome profile, a genuine
    # side effect this check has no business triggering. This only
    # verifies the page itself (and its normal /api/state polling) doesn't
    # destabilize the other open Remote GUI sessions - not the embedded
    # sign-in VNC view, which needs a human to drive on purpose.
    page = await context.new_page()
    watcher = Watcher(page, "accounts")
    await page.goto(f"{base_url}/accounts")
    return watcher


OPENERS = {"vnc": open_vnc, "remote": open_remote, "accounts": open_accounts}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="vnc", help="comma-separated: vnc,remote,accounts")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--poll-seconds", type=float, default=15.0)
    args = ap.parse_args()

    base_url = os.environ.get("ZSDASH_URL")
    username = os.environ.get("ZSDASH_USER")
    password = os.environ.get("ZSDASH_PASS")
    if not (base_url and username and password):
        print("Set ZSDASH_URL, ZSDASH_USER, ZSDASH_PASS", file=sys.stderr)
        return 2

    names = [p.strip() for p in args.pages.split(",") if p.strip()]
    for n in names:
        if n not in OPENERS:
            print(f"unknown page name: {n!r} (choose from {list(OPENERS)})", file=sys.stderr)
            return 2

    deadline = time.monotonic() + args.minutes * 60
    start_wall = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    print(f"[{start_wall}] starting: pages={names} duration={args.minutes}min")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        await login(context, base_url, username, password)

        watchers = [await OPENERS[n](context, base_url) for n in names]
        await asyncio.sleep(3)

        tick = 0
        last_total_closes = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
            tick += 1
            elapsed = args.minutes * 60 - (deadline - time.monotonic())
            total_closes = sum(len(w.close_events) for w in watchers)
            if total_closes > last_total_closes:
                print(f"[+{elapsed:7.1f}s] NEW CLOSE EVENT(S):")
                for w in watchers:
                    for ev in w.close_events:
                        print(f"    {w.name}: {ev}")
                last_total_closes = total_closes
            elif tick % 4 == 0:
                badges = {w.name: await w.badge_text() for w in watchers}
                print(f"[+{elapsed:7.1f}s] ws_count={[(w.name, w.ws_count) for w in watchers]} badges={badges}")

        for w in watchers:
            await w.page.close()
        await browser.close()

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    ok = True
    for w in watchers:
        print(f"{w.name}: {w.ws_count} WebSocket(s) opened, {len(w.close_events)} close/error event(s)")
        for ev in w.close_events:
            print(f"    {ev}")
        if w.close_events:
            ok = False
        if w.ws_count == 0:
            print(f"    WARNING: no /vnc/ws WebSocket ever observed for {w.name} - check selectors still match the current UI")
    print()
    print("PASS - zero close events on every page" if ok else "FAIL - see close events above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
