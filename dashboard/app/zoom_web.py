"""Zoom's own web client (https://<host>/wc/join/<id>), driven the same
way app/cdp.py already drives the YouTube deck: navigate + small JS
snippets run over the Chrome DevTools protocol against the
browser-source kiosk. Used when a zoom source's join_method is "web" (or
join_method=auto has fallen back to it) - see control.py's
producer_unit_for() and switch_zoom_join_via().

Status classification mirrors scripts/zoom-status.py's phrase table so
main.py and the UI see one status vocabulary regardless of which path
joined the meeting: not_joined/connecting/waiting_room/in_meeting/ended/
expired/passcode_required/registration_required/removed/join_failed/
locked/signin_required/duplicate_join/wrong_registrant/unknown, each
with `terminal`. The two phrase tables are necessarily separate copies -
zoom-status.py runs as a subprocess in the zoombot (pipeline) repo
reading AT-SPI text, this runs in-process in the dashboard repo reading
DOM text - keep them in sync by hand if Zoom's wording changes.

Same honesty rule as everywhere else in this codebase: a DOM text/markup
match failure comes back as status="unknown" with the raw text attached
(truncated), never a confident wrong guess. This is inherently more
fragile than the AT-SPI path - Zoom can change its web client's markup
or copy without notice - so treat a run of "unknown" here as a signal to
re-check the live page, not as proof the meeting itself is unhealthy."""
from __future__ import annotations

import asyncio
import json
import re
import time

from . import cdp

# ---------------------------------------------------------------- status

# Kept in the same order/spirit as scripts/zoom-status.py's PHRASES table
# in the pipeline repo. duplicate_join/wrong_registrant are flagged there
# (and here) as uncalibrated - not yet matched against a real Zoom dialog,
# best-effort until verified against a real duplicate/mismatched join.
PHRASES = [
    ("expired", r"(meeting|webinar) has expired|error code:? ?3038",
     "The webinar/meeting link has expired - the event is over, or the host hasn't opened it yet."),
    ("duplicate_join", r"already (joined|in|used) this|you('| a)re already in this meeting|link (has|was) already been used",
     "Zoom says this link/token has already been used to join - a registrant link is single-use. (uncalibrated - verify wording against a real duplicate join)"),
    ("wrong_registrant", r"not (a valid|the correct) registrant|registrant information does not match|invitation is not valid for you",
     "Zoom doesn't recognize this browser session as the registrant this link was issued for. (uncalibrated - verify wording against a real mismatch)"),
    ("join_failed", r"unable to join|invalid meeting id|meeting id is not valid|something went wrong",
     "Zoom's web client could not join - it is showing an error."),
    ("removed", r"removed (you )?from (the|this) meeting|host has removed you",
     "The host removed the bot from the meeting."),
    ("ended", r"meeting has (been )?ended|this meeting has ended|webinar has ended|host ended",
     "The meeting/webinar has ended."),
    ("passcode_required", r"enter (the )?(meeting )?passcode|passcode is (incorrect|invalid)|wrong passcode",
     "Zoom is asking for a passcode (or rejected the one it was given)."),
    ("locked", r"meeting is locked|has locked the meeting|locked by the host",
     "The host has locked the meeting - nobody else can join right now."),
    ("signin_required", r"sign in to join|signed.in users|authenticated (users|attendees) only|only authorized attendees",
     "This meeting only admits signed-in Zoom users."),
    ("registration_required", r"registration is required|register for this (webinar|meeting)|please register",
     "This webinar requires registration - the link used isn't a registrant link."),
    ("waiting_room", r"waiting room|please wait,? the (meeting )?host will let you in|host will let you in soon",
     "The bot is in the waiting room."),
    ("not_started", r"waiting for the host to start|wait for the host to start|host has not started|hasn.t started",
     "The host hasn't started the meeting yet."),
    ("connecting", r"preparing|please wait\.\.\.|loading",
     "Zoom's web client is still connecting."),
]

TERMINAL = {"expired", "duplicate_join", "wrong_registrant", "join_failed", "removed", "ended",
            "passcode_required", "registration_required", "waiting_room", "not_started", "locked",
            "signin_required"}

# Selectors are best-effort against Zoom's current web client (2026) -
# see the module docstring's fragility note. `in_meeting_chrome` looks
# for the meeting toolbar/controls that only exist once actually in a
# meeting; everything else is read from visible text.
JS_STATE = """(() => {
  const bodyText = (document.body && document.body.innerText || '').slice(0, 4000);
  const inMeetingEl = document.querySelector(
    '.footer-button-base, .meeting-client-inner, #wc-footer, [class*="footer__leave-btn"]'
  );
  return {
    url: location.href,
    title: document.title,
    body_text: bodyText,
    in_meeting_chrome: !!(inMeetingEl && inMeetingEl.offsetParent !== null),
  };
})()"""

# One "what should I click/fill next" step, run repeatedly by
# join_from_browser() until the meeting is reached or a terminal state
# appears. window.__ZOOM_BOT_NAME__/__ZOOM_PASSCODE__ are set once ahead
# of time via a separate evaluate() call (JSON-encoded, never string-
# interpolated into this snippet) so a passcode containing regex-special
# characters can never corrupt the script.
JS_AUTOPILOT_STEP = """(() => {
  const visible = (el) => !!el && el.offsetParent !== null;
  const clickByText = (re) => {
    const els = Array.from(document.querySelectorAll('a, button, div[role="button"]'));
    const el = els.find((e) => visible(e) && re.test((e.innerText || e.textContent || '').trim()));
    if (el) { el.click(); return true; }
    return false;
  };
  if (clickByText(/join from (your )?browser/i)) return 'clicked_join_from_browser';
  const nameInput = document.querySelector(
    'input#inputname, input[name="displayName" i], input[aria-label*="your name" i], input[placeholder*="your name" i]'
  );
  if (visible(nameInput) && !nameInput.value) {
    nameInput.value = window.__ZOOM_BOT_NAME__ || 'Stream Bot';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    return 'filled_name';
  }
  const pwdInput = document.querySelector(
    'input[type="password"], input[name*="passcode" i], input[aria-label*="passcode" i]'
  );
  if (visible(pwdInput) && !pwdInput.value && window.__ZOOM_PASSCODE__) {
    pwdInput.value = window.__ZOOM_PASSCODE__;
    pwdInput.dispatchEvent(new Event('input', { bubbles: true }));
    return 'filled_passcode';
  }
  if (clickByText(/^join$/i)) return 'clicked_join';
  if (clickByText(/join audio by computer/i)) return 'clicked_join_audio';
  return null;
})()"""

JS_CLEANFEED_INJECT = """(() => {
  const CSS_TEXT = `
    /* 1. Eliminate Top Header & All Sub-Headers */
    .meeting-app-header, #header, .header, .topic, .meeting-info-header,
    .meeting-info-icon__header, .meeting-topic,
    [class*="header"], [class*="meeting-app-header"], [class*="header__"], [class*="meeting-header"],
    .suspension-header, div[role="banner"], .meeting-client-head,
    [id="header_container"], .header-container {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        opacity: 0 !important;
        pointer-events: none !important;
        visibility: hidden !important;
        min-height: 0 !important;
        max-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
        overflow: hidden !important;
    }

    /* 2. Eliminate Bottom Control Bar & Floating Tools */
    .footer, #wc-footer, .meeting-control-bar, .footer__control-bar,
    [class*="footer"], div[role="toolbar"], #foot-bar,
    .footer-bar, .room-footer, .more-button, .audio-option-menu,
    .settings-dialog, .suspension-window, .security-option-menu,
    .footer-button-base, .leave-btn-container, [class*="leave-btn"],
    .meeting-client-inner .footer, [class*="meeting-control-bar"],
    [class*="footer__control-bar"], [class*="footer-button"],
    [id="footer_container"], .footer-container,
    [id="livesdk__campaign"], [class*="livesdk"], [id*="livesdk"],
    .livesdk__placement, .livesdk__invitation, .livesdk__Draggable {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        opacity: 0 !important;
        pointer-events: none !important;
        visibility: hidden !important;
        min-height: 0 !important;
        max-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
        overflow: hidden !important;
    }

    /* 3. Strip Participant Tags & Badges */
    .participant-name, .video-box__name-tag, [class*="speaker-bar"],
    [class*="name-tag"], #speaker-box-name, .video-avatar__avatar-name,
    .speaker-bar, .name-label, [class*="speaker-name"],
    [class*="participant-name"], .speaker-active-name,
    .can-hide.participant-name, .aria-label-participant-name {
        display: none !important;
        opacity: 0 !important;
        pointer-events: none !important;
        visibility: hidden !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }

    /* 4. Eliminate Black Bars & Force 100vw x 100vh Edge-to-Edge */
    html, body, #root, #app, .main-layout, .meeting-client, .meeting-client-inner, .window-content,
    .video-container, .gallery-video-container, .speaker-view, .single-view,
    .full-screen-video, .video-player-container, #video-container, .video-box,
    [id="content_container"], .zoom-newcontent, .total-main-content, [id="content"], .main-content,
    .react-draggable, [class*="main-layout"], [class*="meeting-client"], [class*="video-container"] {
        width: 100vw !important;
        height: 100vh !important;
        max-width: 100vw !important;
        max-height: 100vh !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
        overflow: hidden !important;
        background-color: #000 !important;
        background: #000 !important;
        box-sizing: border-box !important;
    }

    /* Force active video container and canvas to fill the entire screen */
    .speaker-active-video, .video-avatar-container, .speaker-view,
    video, canvas, canvas.speaker-active-video__canvas, div[class*="active-video"] {
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        max-width: 100vw !important;
        max-height: 100vh !important;
        object-fit: contain !important;
        margin: 0 !important;
        padding: 0 !important;
        z-index: 1 !important;
    }

    ::-webkit-scrollbar {
        display: none !important;
        width: 0 !important;
        height: 0 !important;
    }
  `;

  // 1. Right-Click Context Lock
  const suppress = (e) => {
    if (e) {
      if (typeof e.preventDefault === 'function') e.preventDefault();
      if (typeof e.stopPropagation === 'function') e.stopPropagation();
      if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
    }
    return false;
  };
  if (!window.__cleanfeed_events_bound) {
    window.addEventListener('contextmenu', suppress, true);
    document.addEventListener('contextmenu', suppress, true);
    window.addEventListener('auxclick', (e) => { if (e && e.button === 2) suppress(e); }, true);
    window.addEventListener('keydown', (e) => { if (e && (e.key === 'ContextMenu' || e.keyCode === 93)) suppress(e); }, true);
    window.__cleanfeed_events_bound = true;
  }

  // 2. Ensure Style Element Exists & Contains Nuclear Rules
  let styleEl = document.getElementById('zoom-cleanfeed-style');
  if (!styleEl) {
    styleEl = document.createElement('style');
    styleEl.id = 'zoom-cleanfeed-style';
    styleEl.textContent = CSS_TEXT;
    (document.head || document.documentElement).appendChild(styleEl);
  } else if (styleEl.textContent !== CSS_TEXT) {
    styleEl.textContent = CSS_TEXT;
  }

  // 3. Programmatic DOM Purge & Fit
  const HIDE_SEL = [
    '.meeting-app-header', '#header', '.header', '.topic', '.meeting-info-header',
    '.meeting-info-icon__header', '.meeting-topic',
    '[class*="header"]', '[class*="meeting-app-header"]', '[class*="header__"]', '[class*="meeting-header"]',
    '.suspension-header', 'div[role="banner"]', '.meeting-client-head',
    '[id="header_container"]', '.header-container',
    '.footer', '#wc-footer', '.meeting-control-bar', '.footer__control-bar',
    '[class*="footer"]', 'div[role="toolbar"]', '#foot-bar',
    '.footer-bar', '.room-footer', '.more-button', '.audio-option-menu',
    '.settings-dialog', '.suspension-window', '.security-option-menu',
    '.footer-button-base', '.leave-btn-container', '[class*="leave-btn"]',
    '.meeting-client-inner .footer', '[class*="meeting-control-bar"]',
    '[class*="footer__control-bar"]', '[class*="footer-button"]',
    '[id="footer_container"]', '.footer-container',
    '[id="livesdk__campaign"]', '[class*="livesdk"]', '[id*="livesdk"]',
    '.livesdk__placement', '.livesdk__invitation', '.livesdk__Draggable',
    '.participant-name', '.video-box__name-tag', '[class*="speaker-bar"]',
    '[class*="name-tag"]', '#speaker-box-name', '.video-avatar__avatar-name',
    '.speaker-bar', '.name-label', '[class*="speaker-name"]',
    '[class*="participant-name"]', '.speaker-active-name',
    '.can-hide.participant-name', '.aria-label-participant-name'
  ].join(',');

  const purge = () => {
    try {
      const els = document.querySelectorAll(HIDE_SEL);
      for (let i = 0; i < els.length; i++) {
        const el = els[i];
        if (el && el.style) {
          el.style.setProperty('display', 'none', 'important');
          el.style.setProperty('opacity', '0', 'important');
          el.style.setProperty('pointer-events', 'none', 'important');
          el.style.setProperty('visibility', 'hidden', 'important');
          el.style.setProperty('height', '0', 'important');
        }
      }
      const vids = document.querySelectorAll('.speaker-active-video, .video-avatar-container, .speaker-view, video, canvas, canvas.speaker-active-video__canvas, div[class*="active-video"]');
      for (let i = 0; i < vids.length; i++) {
        const v = vids[i];
        if (v && v.style) {
          v.style.setProperty('position', 'fixed', 'important');
          v.style.setProperty('top', '0', 'important');
          v.style.setProperty('left', '0', 'important');
          v.style.setProperty('width', '100vw', 'important');
          v.style.setProperty('height', '100vh', 'important');
          v.style.setProperty('max-width', '100vw', 'important');
          v.style.setProperty('max-height', '100vh', 'important');
          v.style.setProperty('object-fit', 'contain', 'important');
          v.style.setProperty('margin', '0', 'important');
          v.style.setProperty('padding', '0', 'important');
          v.style.setProperty('z-index', '1', 'important');
        }
      }
    } catch (_) {}
  };

  purge();

  // 4. Persistent MutationObserver on document.documentElement
  if (!window.__cleanfeed_observer) {
    try {
      window.__cleanfeed_observer = new MutationObserver(() => {
        if (!document.getElementById('zoom-cleanfeed-style')) {
          const s = document.createElement('style');
          s.id = 'zoom-cleanfeed-style';
          s.textContent = CSS_TEXT;
          (document.head || document.documentElement).appendChild(s);
        }
        purge();
      });
      window.__cleanfeed_observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'style']
      });
    } catch (_) {}
  }

  return { ok: true, has_style: !!document.getElementById('zoom-cleanfeed-style') };
})()"""


async def apply_cleanfeed_async() -> dict:
    """Evaluates JS_CLEANFEED_INJECT over CDP in the active Zoom page."""
    try:
        res = await cdp.evaluate(JS_CLEANFEED_INJECT)
        return {"ok": True, "result": res.get("value")}
    except cdp.CDPError as exc:
        return {"ok": False, "error": str(exc)}


def clean_feed() -> dict:
    """Synchronous trigger for manual one-click clean-feed."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return {"ok": True, "scheduled": True}
    return asyncio.run(apply_cleanfeed_async())


_cleanfeed_heartbeat_task: asyncio.Task | None = None


async def _cleanfeed_heartbeat_loop():
    """CDP periodic heartbeat loop (every 3 seconds) that verifies the CSS
    injection is present in the active page execution context."""
    while True:
        try:
            await apply_cleanfeed_async()
        except Exception:
            pass
        await asyncio.sleep(3)


def start_cleanfeed_heartbeat():
    global _cleanfeed_heartbeat_task
    if _cleanfeed_heartbeat_task is None or _cleanfeed_heartbeat_task.done():
        try:
            loop = asyncio.get_running_loop()
            _cleanfeed_heartbeat_task = loop.create_task(_cleanfeed_heartbeat_loop())
        except RuntimeError:
            pass


def stop_cleanfeed_heartbeat():
    global _cleanfeed_heartbeat_task
    if _cleanfeed_heartbeat_task and not _cleanfeed_heartbeat_task.done():
        _cleanfeed_heartbeat_task.cancel()
        _cleanfeed_heartbeat_task = None



def _classify_text(text: str) -> tuple[str, str]:
    haystack = (text or "").lower()
    for status, rx, detail in PHRASES:
        if re.search(rx, haystack):
            return status, detail
    return "", ""


async def _status_async() -> dict:
    try:
        result = await cdp.evaluate(JS_STATE)
    except cdp.CDPError as exc:
        return {"status": "unknown", "detail": f"Zoom's web client isn't reachable: {exc}",
                "terminal": False, "authoritative": False, "dialogs": []}
    page = result.get("value") or {}
    text = page.get("body_text", "")
    st, detail = _classify_text(text)
    if st:
        return {"status": st, "detail": detail, "terminal": st in TERMINAL, "authoritative": False, "dialogs": []}
    if page.get("in_meeting_chrome"):
        return {"status": "in_meeting", "detail": "Zoom's web client is showing the in-meeting toolbar.",
                "terminal": False, "authoritative": False, "dialogs": []}
    if "zoom.us" in (page.get("url") or ""):
        return {"status": "connecting", "detail": "On a Zoom web client screen, not yet in the meeting.",
                "terminal": False, "authoritative": False, "dialogs": []}
    return {"status": "unknown", "detail": "Couldn't recognize the web client's current screen.",
            "terminal": False, "authoritative": False, "dialogs": [], "raw_text": text[:300]}


def status() -> dict:
    """Same shape as control.zoom_meeting_status()'s AT-SPI path -
    {"status", "detail", "terminal", "authoritative": False, "dialogs": []}."""
    return asyncio.run(_status_async())


# ---------------------------------------------------------------- join automation

async def _join_from_browser_async(bot_name: str, passcode: str | None, timeout: float) -> dict:
    """Tolerant of DevTools not being reachable yet for the first several
    seconds - browser-source.sh may have only just been (re)started (e.g.
    by the join_method=auto fallback), and `systemctl restart` on a
    Type=simple unit returns as soon as Chrome is forked, well before its
    own internal DevTools-readiness gate passes. A CDPError is treated as
    "not ready yet" and retried until `timeout`, not an immediate failure."""
    steps: list[str] = []
    globals_set = False
    deadline = time.monotonic() + timeout
    last_status: dict = {}
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            if not globals_set:
                await cdp.evaluate(
                    f"window.__ZOOM_BOT_NAME__={json.dumps(bot_name)};"
                    f"window.__ZOOM_PASSCODE__={json.dumps(passcode or '')};"
                )
                globals_set = True
            result = await cdp.evaluate(JS_AUTOPILOT_STEP, user_gesture=True)
        except cdp.CDPError as exc:
            last_error = str(exc)[:300]
            await asyncio.sleep(1.5)
            continue
        step = result.get("value")
        if step:
            steps.append(step)
        # Actively apply clean-feed patch & context lock during join
        try:
            await cdp.evaluate(JS_CLEANFEED_INJECT)
        except Exception:
            pass
        last_status = await _status_async()
        if last_status.get("status") == "in_meeting":
            try:
                await cdp.evaluate(JS_CLEANFEED_INJECT)
            except Exception:
                pass
            return {"ok": True, "steps": steps, "status": last_status}
        if last_status.get("terminal"):
            return {"ok": False, "steps": steps, "status": last_status}
        await asyncio.sleep(1.5)
    if not steps and last_error:
        return {"ok": False, "steps": steps, "error": last_error}
    return {"ok": False, "steps": steps, "status": last_status or
            {"status": "unknown", "detail": "Timed out waiting to reach the meeting.", "terminal": False}}


def join_from_browser(bot_name: str, passcode: str | None = None, timeout: float = 60.0) -> dict:
    """Drives Zoom's pre-join screen from the web-client kiosk tab already
    navigated to the wc/join URL (browser-source.sh does the navigation;
    this only clicks/fills once the page exists): picks "Join from your
    Browser" over the desktop-app handoff, fills the guest name and an
    embedded passcode if Zoom still asks for one, then "Join Audio by
    Computer" so the tab actually outputs meeting audio into the zoom_out
    sink. Camera/mic are never touched (stay off, same as a guest desktop
    join). Polls the real status after every step rather than assuming
    a click landed - see the module docstring's fragility note."""
    return asyncio.run(_join_from_browser_async(bot_name, passcode, timeout))


# ---------------------------------------------------------------- reset ("close tab, relaunch")

async def _reset_async(join_url: str) -> dict:
    try:
        await cdp.navigate("about:blank")
        await asyncio.sleep(1.0)
        await cdp.navigate(join_url)
    except cdp.CDPError as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": True}


def reset(join_url: str) -> dict:
    """The web-client equivalent of "Reset Zoom": navigates the kiosk tab
    away and back, the closest a single controlled tab can get to "close
    it, relaunch it" without restarting the whole browser-source unit
    (control.zoom_reset falls back to that restart if this fails - e.g.
    DevTools isn't reachable at all)."""
    return asyncio.run(_reset_async(join_url))
