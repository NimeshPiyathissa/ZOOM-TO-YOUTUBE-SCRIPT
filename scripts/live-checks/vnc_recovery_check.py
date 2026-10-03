#!/usr/bin/env python3
"""Connect to /vnc in a headless browser and report every status-badge
transition for a given duration, so a deliberate server-side disruption
(restarting x11vnc, restarting dashboard.service) triggered by hand (or by
another script) during that window shows up as a timestamped sequence:
Connecting -> Connected -> Reconnecting(attempt, delay) -> ... -> Connected.

Needs a real deployed dashboard + login - see README.md in this directory.

Usage:
    ZSDASH_URL=https://your-dashboard ZSDASH_USER=... ZSDASH_PASS=... \
        python vnc_recovery_check.py --seconds 120
    # then, in another terminal, restart x11vnc or dashboard.service and
    # watch the transitions print here.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--poll-seconds", type=float, default=0.5)
    args = ap.parse_args()

    base_url = os.environ.get("ZSDASH_URL")
    username = os.environ.get("ZSDASH_USER")
    password = os.environ.get("ZSDASH_PASS")
    if not (base_url and username and password):
        print("Set ZSDASH_URL, ZSDASH_USER, ZSDASH_PASS", file=sys.stderr)
        return 2

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        await page.goto(f"{base_url}/login")
        await page.fill('input[name="username"]', username)
        await page.fill('input[name="password"]', password)
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_load_state("networkidle")

        vnc_page = await context.new_page()
        await vnc_page.goto(f"{base_url}/vnc")
        try:
            connect_btn = vnc_page.locator("#vnc-overlay-btn")
            if await connect_btn.is_visible(timeout=3000):
                await connect_btn.click()
        except Exception:
            pass

        badge = vnc_page.locator("#vnc-badge-text")
        start = time.monotonic()
        last_text = None
        transitions: list[tuple[float, str]] = []
        print(f"watching for {args.seconds:.0f}s - restart x11vnc or dashboard.service now if that's the point of this run")
        while time.monotonic() - start < args.seconds:
            try:
                text = (await badge.text_content()) or ""
            except Exception:
                text = "<page gone>"
            if text != last_text:
                elapsed = time.monotonic() - start
                transitions.append((elapsed, text))
                print(f"[+{elapsed:7.1f}s] badge -> {text!r}")
                last_text = text
            await asyncio.sleep(args.poll_seconds)

        await browser.close()

    print()
    print("=" * 60)
    print("TRANSITIONS")
    print("=" * 60)
    for elapsed, text in transitions:
        print(f"[+{elapsed:7.1f}s] {text}")
    ended_connected = transitions and "Connected" in transitions[-1][1]
    print()
    print("ended CONNECTED" if ended_connected else "did NOT end connected - see transitions above")
    return 0 if ended_connected else 1


if __name__ == "__main__":
    # Exit codes: 0 = ended Connected, 1 = ran fine but did NOT end
    # Connected (a real observed failure - what this script exists to
    # catch), 2 = missing env vars, 3 = the script itself crashed
    # (Playwright/network error etc.) before it could finish watching -
    # distinct from 1 so "the test failed" and "the test didn't run"
    # don't get read as the same thing.
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"CRASHED: {exc!r}", file=sys.stderr)
        raise SystemExit(3)
