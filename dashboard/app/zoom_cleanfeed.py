"""Clean feed for the Zoom web client, rebuilt so it can no longer break meeting detection or the join page.

What was wrong with the old version (found on production, 2026-11-05):
  * It hid `[class*="header"]` / `[class*="footer"]` / `#wc-footer` / the Leave button on EVERY zoom.us page with
    display:none !important, so the footer was never visible and the dashboard (which looked for a visible footer)
    reported "connecting" for a whole meeting.
  * It also ran on the pre-join / waiting pages, where those generic selectors can hide the very form that has to be
    filled in to join, and it forced html/body/video to 100vw x 100vh on a black background everywhere (black bars).
  * A MutationObserver over the whole subtree re-ran a querySelectorAll purge writing inline styles on every change:
    constant main-thread work in a software-rendered kiosk.

Now:
  * Every rule is scoped under html[data-zs-cleanfeed="on"], an attribute a tiny script sets ONLY while a meeting is on
    screen (a Leave control exists and no join form does) and clears otherwise. Pre-join, waiting-room, error and
    sign-in pages are never touched.
  * Pure CSS; the script only toggles one attribute (cheap, every 1.5 s) and suppresses the context menu.
  * Hidden elements stay in the DOM, and meeting detection (app/zoom_state.py) does not look at visibility at all.
  * Generic [class*="header"]/[class*="footer"] rules are limited to inside .meeting-client and never inside dialogs.

The same text ships in the Chrome extension (extensions/zoom-cleanfeed/*); tests/test_zoom_state.py asserts they match.
"""
import json

_ON = 'html[data-zs-cleanfeed="on"]'
_NOT_DIALOG = ':not([role="dialog"]):not([role="dialog"] *)'


def _scoped(selectors, inside="", suffix=""):
    return ",\n".join(f"{_ON} {inside}{s}{suffix}" for s in selectors)


_HEADERS = [".meeting-app-header", "#header_container", ".header-container", ".meeting-info-header", ".meeting-info-icon__header",
            ".meeting-topic", ".suspension-header", ".meeting-client-head", 'div[role="banner"]']
_HEADERS_GENERIC = ['[class*="meeting-app-header"]', '[class*="meeting-header"]', '[class*="header"]']
_FOOTERS = ["#wc-footer", ".footer__control-bar", ".meeting-control-bar", "#foot-bar", ".footer-bar", ".room-footer",
            "#footer_container", ".footer-container", '[class*="livesdk"]', '[id*="livesdk"]']
_FOOTERS_GENERIC = ['[class*="footer"]', 'div[role="toolbar"]']
_NAMES = [".participant-name", ".video-box__name-tag", '[class*="speaker-bar"]', '[class*="name-tag"]', "#speaker-box-name",
          ".video-avatar__avatar-name", ".speaker-bar", ".name-label", '[class*="speaker-name"]',
          '[class*="participant-name"]', ".speaker-active-name"]

_HIDE = "{\n  display: none !important;\n  opacity: 0 !important;\n  pointer-events: none !important;\n}"

CSS_TEXT = "\n".join([
    '/* Zoom clean feed - applies ONLY while a meeting is on screen (html[data-zs-cleanfeed="on"]). Generated text; do not hand-edit. */',
    _scoped(_HEADERS) + ",\n" + _scoped(_HEADERS_GENERIC, ".meeting-client ", _NOT_DIALOG) + " " + _HIDE,
    _scoped(_FOOTERS) + ",\n" + _scoped(_FOOTERS_GENERIC, ".meeting-client ", _NOT_DIALOG) + " " + _HIDE,
    _scoped(_NAMES) + " " + _HIDE,
    _ON + " ::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }",
    # Fit the picture to the window without stretching it (object-fit keeps the shape; any bars are the picture's own).
    _ON + " .meeting-client video, " + _ON + " .meeting-client canvas { object-fit: contain !important; }",
]) + "\n"

# Sets/clears the attribute. Runs in the page (extension content script AND the CDP fallback) - no heavy work, no subtree
# observers, nothing that touches the DOM except one attribute on <html>.
STATE_SCRIPT = r"""
(function () {
  'use strict';
  if (window.__zsCleanFeed) { window.__zsCleanFeed.update(); return; }
  var root = document.documentElement;
  function inMeeting() {
    var leave = document.querySelector('[class*="footer__leave-btn"], #wc-footer button[aria-label="Leave"], button[aria-label="Leave"]');
    var joinForm = document.querySelector('#inputname, input[name="displayName"], input[type="password"]');
    return !!leave && !joinForm;
  }
  function update() {
    var on = !window.__zsCleanFeedOff && inMeeting();
    if (on) { if (root.getAttribute('data-zs-cleanfeed') !== 'on') root.setAttribute('data-zs-cleanfeed', 'on'); }
    else if (root.hasAttribute('data-zs-cleanfeed')) { root.removeAttribute('data-zs-cleanfeed'); }
  }
  var suppress = function (e) { try { e.preventDefault(); e.stopPropagation(); } catch (_) {} return false; };
  window.addEventListener('contextmenu', suppress, true);
  window.__zsCleanFeed = { update: update, off: function () { window.__zsCleanFeedOff = true; update(); },
                           on: function () { window.__zsCleanFeedOff = false; update(); } };
  update();
  setInterval(update, 1500);
})();
"""

# The CDP fallback: the same CSS (as a <style>) plus the same script, for a browser whose extension did not load.
INJECT_JS = (
    "(() => {\n"
    "  const CSS_TEXT = " + json.dumps(CSS_TEXT) + ";\n"
    "  let styleEl = document.getElementById('zoom-cleanfeed-style');\n"
    "  if (!styleEl) { styleEl = document.createElement('style'); styleEl.id = 'zoom-cleanfeed-style'; (document.head || document.documentElement).appendChild(styleEl); }\n"
    "  if (styleEl.textContent !== CSS_TEXT) styleEl.textContent = CSS_TEXT;\n"
    + STATE_SCRIPT +
    "  return { ok: true, has_style: !!document.getElementById('zoom-cleanfeed-style'), meeting: document.documentElement.getAttribute('data-zs-cleanfeed') === 'on' };\n"
    "})()"
)
