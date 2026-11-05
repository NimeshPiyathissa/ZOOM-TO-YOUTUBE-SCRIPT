/* Zoom clean-feed content script (generated from dashboard/app/zoom_cleanfeed.py - do not hand-edit).
   Only toggles html[data-zs-cleanfeed]; all hiding is CSS in cleanfeed.css and applies only while a meeting is on screen. */

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
