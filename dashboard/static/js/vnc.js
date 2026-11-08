// Remote GUI (/vnc) client using noVNC with auto-reconnect, fit-to-window scaling,
// and automated VNC authentication. RFB construction/event-wiring itself lives in
// vnc-embed.js's connectVnc() (shared with the Accounts sign-in panel, the /remote
// Interact overlay and the global quick-peek overlay) - this file only owns the
// page-level behaviour on top: the status overlay, auto-reconnect loop, view
// modes (Smart Fit / Fit / Fill / 1:1 - geometry in vnc-fit.js), fullscreen
// toggle and quick actions.
import { connectVnc, promptText, reconnectDelayMs, CLOSE_AUTH_FAILED } from '/static/js/vnc-embed.js';
import { DEFAULT_MODE, MODE_LABELS, normalizeMode, computeLayout, describeLayout, measureBars } from '/static/js/vnc-fit.js';

const $ = (id) => document.getElementById(id);
const stage = $("vnc-screen");
const target = $("vnc-frame");
const viewport = $("vnc-viewport");
const device = $("vnc-device");
const header = document.querySelector(".vnc-head");
const quickBar = device?.querySelector(".vnc-quick-bar");
const fitText = $("vnc-fit-text");
const modeButtons = Array.from(document.querySelectorAll("#vnc-view-modes [data-view]"));
const resetBtn = $("vnc-btn-reset");
const badge = $("vnc-badge");
const badgeText = $("vnc-badge-text");
const overlay = $("vnc-overlay-msg");
const overlayText = $("vnc-overlay-text");
const overlaySub = $("vnc-overlay-sub");
const overlayBtn = $("vnc-overlay-btn");
const spinner = $("vnc-spinner");
const scaleBtn = $("vnc-btn-scale");
const reconnectBtn = $("vnc-btn-reconnect");
const fullscreenBtn = $("vnc-btn-fullscreen");

// No password is ever embedded in the page: /vnc/ws authenticates to
// x11vnc server-side and offers the browser only the "None" security
// type, so getPassword() below should never actually be invoked. It
// stays only as a fallback (remembered in memory for this tab only,
// never persisted or sent to the server) in case that ever changes.
let vncPassword = null;

let rfb = null;
const VIEW_KEY = "zs.vnc.view";
const FB_KEY = "zs.vnc.fb";
let viewMode = DEFAULT_MODE;
try { viewMode = normalizeMode(localStorage.getItem(VIEW_KEY)); } catch (err) { /* storage blocked */ }
// Last framebuffer size seen, so the panel already has the right shape
// before the first connect. Xvfb :99 is 1920x1080.
let lastFb = { w: 1920, h: 1080 };
try {
  const saved = JSON.parse(localStorage.getItem(FB_KEY) || "null");
  if (saved && saved.w > 0 && saved.h > 0) lastFb = saved;
} catch (err) { /* ignore */ }
let reconnectTimer = null;
let reconnectAttempt = 0;
let intentionalDisconnect = false;
let isConnected = false;
// Bumped on every connect() call. connect() replaces `rfb` by calling
// disconnect() on whatever the old one was - but that old RFB object's
// own 'disconnect' event fires asynchronously, sometimes arriving after
// a *new* connection has already been created (even after it's already
// healthy). Without this, that stale event reads intentionalDisconnect
// as false (already reset for the new attempt) and schedules its own
// reconnectTimer, silently overwriting/orphaning the new connection's
// own timer - which then fires later and kills a perfectly good session.
// Root cause of "connects fine, then drops a moment later, forever" with
// nothing resembling a real failure anywhere in the server-side logs.
// Each onStatus closure captures its own connectionId at creation time
// and ignores itself once a newer connect() has superseded it.
let connectionId = 0;

const post = (url, body) => apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined });

function setStatus(state, message, subtext = "") {
  if (!badge) return;
  if (state === "connected") {
    badge.className = "badge badge-live";
    badgeText.textContent = "Connected · :99";
    if (overlay) overlay.hidden = true;
  } else if (state === "connecting") {
    badge.className = "badge badge-inactive";
    badgeText.textContent = "Connecting…";
    if (overlay) {
      overlay.hidden = false;
      if (spinner) spinner.hidden = false;
      if (overlayText) overlayText.textContent = message || "Connecting to remote desktop…";
      if (overlaySub) overlaySub.textContent = subtext || "Establishing secure WebSocket connection to display :99";
      if (overlayBtn) overlayBtn.hidden = true;
    }
  } else if (state === "reconnecting") {
    badge.className = "badge badge-warning";
    badgeText.textContent = message || "Reconnecting in 2s…";
    if (overlay) {
      overlay.hidden = false;
      if (spinner) spinner.hidden = false;
      if (overlayText) overlayText.textContent = message || "Connection lost. Reconnecting…";
      if (overlaySub) overlaySub.textContent = subtext || "Retrying in 2 seconds…";
      if (overlayBtn) overlayBtn.hidden = true;
    }
  } else {
    badge.className = "badge badge-inactive";
    badgeText.textContent = "Disconnected";
    if (overlay) {
      overlay.hidden = false;
      if (spinner) spinner.hidden = true;
      if (overlayText) overlayText.textContent = message || "Disconnected from remote desktop";
      if (overlaySub) overlaySub.textContent = subtext || "Click Connect to start a new session";
      if (overlayBtn) overlayBtn.hidden = false;
    }
  }
}

function connect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (rfb) {
    try { rfb.disconnect(); } catch (err) { /* ignore */ }
    rfb = null;
  }
  target.innerHTML = "";

  const myConnectionId = ++connectionId;
  intentionalDisconnect = false;
  setStatus("connecting");

  try {
    rfb = connectVnc(target, {
      path: "/vnc/ws",
      scaleViewport: viewMode !== "native",
      qualityLevel: 6,
      showDotCursor: true,
      getPassword: async () => {
        if (vncPassword) return vncPassword;
        const pass = await promptText("Enter the VNC password to connect to display :99.");
        if (pass == null) {
          intentionalDisconnect = true;
          setStatus("disconnected", "Password cancelled", "Enter password to connect");
          return null;
        }
        vncPassword = pass;
        return pass;
      },
      onStatus: (state, message, clean, code) => {
        if (myConnectionId !== connectionId) {
          // Stale event from an RFB instance connect() has since replaced
          // (see connectionId's header comment) - this is not the active
          // connection any more, so acting on it would only corrupt the
          // real one's state (orphaned reconnect timers, wrong badge).
          return;
        }
        if (state === "connected") {
          isConnected = true;
          reconnectAttempt = 0;
          // A reconnectTimer can already be pending here: a brief early
          // hiccup during this same connection attempt can fire
          // onStatus("disconnected", clean=false) - which schedules a
          // retry - moments before the attempt actually completes and
          // reaches "connected". Nothing previously cancelled that timer
          // once we *did* connect, so ~1s later it fired anyway and tore
          // down an otherwise healthy session, forever, in a tight loop.
          if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
          }
          setStatus("connected");
          // The framebuffer size is known now (ServerInit): re-fit to it.
          applyView();
          try { rfb.focus({ preventScroll: true }); } catch (err) { /* older noVNC */ }
          post("/api/vnc/rate", { mode: "fast" }).catch(() => {});
          if (typeof announce === "function") announce("Remote GUI connected");
          return;
        }
        if (state === "securityfailure") {
          // A rejected password must never auto-retry - that would hammer
          // the proxy with the same bad password forever.
          vncPassword = null;
          isConnected = false;
          intentionalDisconnect = true;
          setStatus("disconnected", "Authentication failed", "VNC password was rejected. Check settings.");
          return;
        }
        // state === "disconnected"
        const wasConnected = isConnected;
        isConnected = false;
        if (intentionalDisconnect) {
          // Whatever intentional-disconnect path set (e.g. "Password
          // cancelled") already painted the overlay - don't stomp it with
          // the generic disconnect message that immediately follows it.
          return;
        }
        if (code === CLOSE_AUTH_FAILED) {
          // vnc_proxy.py couldn't authenticate to x11vnc with the stored
          // password - every retry would fail identically, so this is
          // terminal like securityfailure above, not a transient drop.
          // Setting intentionalDisconnect also keeps visibilitychange's
          // auto-reconnect from hammering the same bad password later.
          intentionalDisconnect = true;
          setStatus("disconnected", "VNC authentication failed", "The stored VNC password was rejected. Check Configuration.");
          return;
        }
        // Deliberately NOT branching on noVNC's `clean` flag here. It
        // only ever reports unclean (_rfbCleanDisconnect = false) from
        // inside noVNC's own _fail(), which exclusively fires during the
        // handshake/connecting phase - a server closing an already-
        // CONNECTED session (x11vnc restarting, the proxy losing its
        // upstream) goes through _socketClose()'s plain 'connected' case
        // instead, which always reports clean=true regardless of the
        // real WebSocket close code or reason. The close `code` above
        // (from vnc_proxy.py's CLOSE_* constants, forwarded by noVNC's
        // local patch - see vnc-embed.js) is the trustworthy signal for
        // the one case that must not retry; intentionalDisconnect is the
        // signal for every case where *we* chose to stop.
        //
        // Auto-reconnect with backoff (1s, 2s, 4s... capped at 20s) -
        // retries forever rather than giving up, but doesn't hammer the
        // proxy every couple of seconds during a longer outage.
        reconnectAttempt += 1;
        const delay = reconnectDelayMs(reconnectAttempt);
        setStatus("reconnecting", `Reconnecting in ${Math.round(delay / 1000)}s… (attempt ${reconnectAttempt})`, wasConnected ? "Connection lost unexpectedly" : "Could not establish initial connection");
        reconnectTimer = setTimeout(() => { connect(); }, delay);
      },
    });
  } catch (err) {
    setStatus("disconnected", "Failed to start viewer", err.message);
  }
}

// Controls: Reconnect
reconnectBtn?.addEventListener("click", () => {
  intentionalDisconnect = false;
  reconnectAttempt = 0;
  connect();
});
overlayBtn?.addEventListener("click", () => {
  intentionalDisconnect = false;
  reconnectAttempt = 0;
  connect();
});

// ---------------------------------------------------------------- view modes
// The card (#vnc-viewport) is a slot whose height runs to the bottom of the
// window; vnc-fit.js turns slot + framebuffer size into the panel, stage and
// noVNC frame boxes; the result is then read back from the real canvas rect
// so the status line reports what is on screen, not what was planned.
// (The old "Fit Window" button toggled fit OFF - its label named the current
// state - and the card had no definite height, so the canvas pushed the
// page past the bottom of the window. See vnc-fit.js's header.)
const BOTTOM_GAP = 16;     // px kept free under the panel (matches --shell-gap)
const MIN_SLOT_H = 260;

function fbSize() {
  const w = rfb && rfb._fbWidth, h = rfb && rfb._fbHeight;  // noVNC 1.4 has no public getter
  if (w > 0 && h > 0 && (w !== lastFb.w || h !== lastFb.h)) {
    lastFb = { w, h };
    try { localStorage.setItem(FB_KEY, JSON.stringify(lastFb)); } catch (err) { /* ignore */ }
  }
  return lastFb;
}

function sizeSlot() {
  if (isFullscreen()) { viewport.style.removeProperty("--vnc-slot-h"); return; }
  const top = viewport.getBoundingClientRect().top + window.scrollY;
  // Phones: the fixed Go Live bar covers the bottom of the window.
  const dock = document.querySelector(".mobile-bar");
  const dockH = dock && getComputedStyle(dock).display !== "none" ? window.innerHeight - dock.getBoundingClientRect().top : 0;
  const h = Math.max(MIN_SLOT_H, Math.floor(window.innerHeight - top - BOTTOM_GAP - Math.max(0, dockH)));
  viewport.style.setProperty("--vnc-slot-h", h + "px");
}

function layoutOnce(barH) {
  const fb = fbSize();
  // The panel's own border sits inside its box; leaving it out made the
  // canvas 2 px wider than the stage and clipped the remote screen's edge.
  const cs = getComputedStyle(device);
  const bw = (parseFloat(cs.borderLeftWidth) || 0) + (parseFloat(cs.borderRightWidth) || 0);
  const bh = (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
  const layout = computeLayout({
    mode: viewMode, slotW: viewport.clientWidth - bw, slotH: viewport.clientHeight - bh,
    barH, fbW: fb.w, fbH: fb.h, fullscreen: isFullscreen(),
  });
  const shrink = viewMode === "smart" && layout.barDocked;
  device.style.width = shrink ? layout.device.w + bw + "px" : "";
  device.style.height = shrink ? layout.device.h + bh + "px" : "";
  device.classList.toggle("bar-overlay", !layout.barDocked);
  if (!layout.barDocked) device.classList.remove("show-bar");
  target.style.left = layout.frame.x + "px";
  target.style.top = layout.frame.y + "px";
  target.style.width = layout.frame.w + "px";
  target.style.height = layout.frame.h + "px";
  return layout;
}

let lastLayout = null;

function verifyView() {
  const fb = fbSize();
  const canvas = target.querySelector("canvas");
  // Fill crops by design and 1:1 pans, so "bars" are only measured for the fits.
  const m = canvas && isConnected && viewMode !== "fill" && viewMode !== "native"
    ? measureBars(stage.getBoundingClientRect(), canvas.getBoundingClientRect()) : null;
  const msg = lastLayout ? describeLayout(lastLayout, fb.w, fb.h, m) : "";
  const borders = m ? m.bars : { x: 0, y: 0 };
  viewport.dataset.bars = `${borders.x},${borders.y}`;
  viewport.dataset.crop = lastLayout ? `${lastLayout.crop.x},${lastLayout.crop.y}` : "0,0";
  const cropped = lastLayout?.mode === "fill" && (lastLayout.crop.x > 0 || lastLayout.crop.y > 0);
  const warn = cropped || borders.x > 1 || borders.y > 1;
  if (fitText) {
    fitText.textContent = msg;
    fitText.title = msg;
    fitText.classList.toggle("is-warn", warn);
  }
  return { msg, warn };
}

function applyView({ report = false } = {}) {
  if (!viewport || !device || !target) return Promise.resolve(null);
  sizeSlot();
  // The quick bar wraps when the panel narrows, which changes its height,
  // which changes the panel width again - settle it in a few passes.
  let barH = quickBar ? quickBar.offsetHeight : 0;
  let layout = layoutOnce(barH);
  for (let i = 0; i < 3 && quickBar && layout.barDocked; i += 1) {
    const h = quickBar.offsetHeight;
    if (h === barH) break;
    barH = h;
    layout = layoutOnce(barH);
  }
  lastLayout = layout;
  const fb = fbSize();
  viewport.dataset.view = viewMode;
  viewport.dataset.fb = `${fb.w}x${fb.h}`;
  if (rfb) {
    const native = viewMode === "native";
    try {
      // 1:1 is bigger than the box: let it be dragged around rather than
      // silently cropped to the centre.
      rfb.clipViewport = native;
      rfb.dragViewport = native;
      // Assigning re-runs noVNC's autoscale against the frame's new size
      // right away instead of waiting for its own ResizeObserver.
      rfb.scaleViewport = !native;
    } catch (err) { /* rfb torn down */ }
  }
  modeButtons.forEach((b) => {
    const on = b.dataset.view === viewMode;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  // Read back after noVNC has rescaled (its own pass runs in a rAF).
  return new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(() => {
      const res = verifyView();
      if (report && typeof toast === "function") {
        if (!isConnected) toast(`${MODE_LABELS[viewMode]} selected - it applies when the remote screen connects`, "info");
        else toast(res.msg, res.warn ? "warn" : "ok");
      }
      resolve(res);
    }, 30)));
  });
}

let relayoutQueued = false;
function queueRelayout() {
  if (relayoutQueued) return;
  relayoutQueued = true;
  requestAnimationFrame(() => { relayoutQueued = false; applyView(); });
}

function setViewMode(mode) {
  viewMode = normalizeMode(mode);
  try { localStorage.setItem(VIEW_KEY, viewMode); } catch (err) { /* ignore */ }
  return applyView({ report: true });
}

modeButtons.forEach((b) => b.addEventListener("click", () => setViewMode(b.dataset.view)));

// Fit Window: re-measure window, panel and framebuffer and fit again. From
// Fill or 1:1 it goes back to Smart Fit, the fit that never crops.
scaleBtn?.addEventListener("click", () => {
  if (viewMode === "fill" || viewMode === "native") setViewMode("smart");
  else applyView({ report: true });
});

// Reset: the default view - Smart Fit, out of fullscreen, page at the top.
resetBtn?.addEventListener("click", async () => {
  try { localStorage.removeItem(VIEW_KEY); } catch (err) { /* ignore */ }
  viewMode = DEFAULT_MODE;
  if (isFullscreen()) {
    try {
      if (document.exitFullscreen) await document.exitFullscreen();
      else if (document.webkitExitFullscreen) await document.webkitExitFullscreen();
    } catch (err) { /* ignore */ }
  }
  window.scrollTo({ top: 0 });
  applyView({ report: true });
});

if (typeof ResizeObserver === "function") {
  // Slot width (sidebar collapse, fullscreen) and header height (toolbar
  // wrapping moves the slot's top) both change the available area.
  const ro = new ResizeObserver(queueRelayout);
  ro.observe(viewport);
  if (header) ro.observe(header);
}
window.addEventListener("resize", queueRelayout);

// Fullscreen Smart Fit: the quick bar floats over the top edge. It shows only
// when the pointer touches the very top (or a control in it has focus) and
// hides again once the pointer leaves it - a wider trigger band covered
// Zoom's own top-left controls every time the pointer passed over them.
const BAR_REVEAL_PX = 6;
let barHideTimer = null;
function showBarBriefly(ms = 2500) {
  if (!device.classList.contains("bar-overlay")) return;
  device.classList.add("show-bar");
  clearTimeout(barHideTimer);
  barHideTimer = setTimeout(() => device.classList.remove("show-bar"), ms);
}
viewport?.addEventListener("pointermove", (e) => {
  if (device.classList.contains("bar-overlay") && e.clientY - device.getBoundingClientRect().top <= BAR_REVEAL_PX) showBarBriefly();
});
quickBar?.addEventListener("pointerenter", () => { if (device.classList.contains("show-bar")) clearTimeout(barHideTimer); });
quickBar?.addEventListener("pointerleave", () => { if (device.classList.contains("show-bar")) showBarBriefly(600); });

// Controls: Fullscreen toggle
function isFullscreen() {
  return document.fullscreenElement === viewport || document.webkitFullscreenElement === viewport;
}
async function toggleFullscreen() {
  try {
    if (isFullscreen()) {
      if (document.exitFullscreen) await document.exitFullscreen();
      else if (document.webkitExitFullscreen) await document.webkitExitFullscreen();
    } else {
      if (viewport.requestFullscreen) await viewport.requestFullscreen({ navigationUI: "hide" });
      else if (viewport.webkitRequestFullscreen) await viewport.webkitRequestFullscreen();
    }
  } catch (err) {
    if (typeof toast === "function") toast("Fullscreen failed: " + err.message, "err");
  }
}
fullscreenBtn?.addEventListener("click", toggleFullscreen);

function onFullscreenChange() {
  const on = isFullscreen();
  viewport.classList.toggle("is-fullscreen", on);
  applyView().then(() => { if (on) showBarBriefly(3000); });
  fullscreenBtn?.setAttribute("aria-pressed", String(on));
  fullscreenBtn?.classList.toggle("btn-primary", on);
  fullscreenBtn?.classList.toggle("btn-secondary", !on);
}
document.addEventListener("fullscreenchange", onFullscreenChange);
document.addEventListener("webkitfullscreenchange", onFullscreenChange);

// Quick actions
$("vnc-q-zoom")?.addEventListener("click", async () => {
  try {
    const res = await post("/api/window/focus", { which: "zoom" });
    if (typeof toast === "function") toast("Focused Zoom window: " + (res?.title || "Zoom"));
  } catch (err) {
    if (typeof toast === "function") toast(err.message, "err");
  }
});

$("vnc-q-browser")?.addEventListener("click", async () => {
  try {
    const res = await post("/api/browser/open", {});
    if (typeof toast === "function") toast("Browser: " + (res?.signed_in_as ? "Signed in as " + res.signed_in_as : "Opened"));
  } catch (err) {
    if (typeof toast === "function") toast(err.message, "err");
  }
});

$("vnc-q-cad")?.addEventListener("click", () => {
  if (rfb && isConnected) {
    const KS_Ctrl = 0xffe3, KS_Alt = 0xffe9, KS_Delete = 0xffff;
    rfb.sendKey(KS_Ctrl, "ControlLeft", true);
    rfb.sendKey(KS_Alt, "AltLeft", true);
    rfb.sendKey(KS_Delete, "Delete", true);
    rfb.sendKey(KS_Delete, "Delete", false);
    rfb.sendKey(KS_Alt, "AltLeft", false);
    rfb.sendKey(KS_Ctrl, "ControlLeft", false);
    if (typeof toast === "function") toast("Sent Ctrl+Alt+Del");
  }
});

// Focus canvas on click
target.addEventListener("click", () => {
  if (rfb && isConnected) {
    try { rfb.focus(); } catch (err) {}
  }
});

// Visibility & cleanup
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !isConnected && !intentionalDisconnect) {
    connect();
  }
});

window.addEventListener("pagehide", () => {
  if (isConnected) {
    try {
      fetch("/api/vnc/rate", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": typeof csrfToken === "function" ? csrfToken() : "",
        },
        body: JSON.stringify({ mode: "slow" }),
      });
    } catch (err) {}
    if (rfb) {
      try { rfb.disconnect(); } catch (err) {}
    }
  }
});

// Initial layout (framebuffer shape from the last visit), then connect.
applyView();
connect();
