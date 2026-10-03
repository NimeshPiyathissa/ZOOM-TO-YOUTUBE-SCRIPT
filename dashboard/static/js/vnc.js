// Remote GUI (/vnc) client using noVNC with auto-reconnect, fit-to-window scaling,
// and automated VNC authentication. RFB construction/event-wiring itself lives in
// vnc-embed.js's connectVnc() (shared with the Accounts sign-in panel, the /remote
// Interact overlay and the global quick-peek overlay) - this file only owns the
// page-level behaviour on top: the status overlay, auto-reconnect loop, fullscreen
// toggle and quick actions.
import { connectVnc, promptText, reconnectDelayMs } from '/static/js/vnc-embed.js';

const $ = (id) => document.getElementById(id);
const target = $("vnc-screen");
const viewport = $("vnc-viewport");
const badge = $("vnc-badge");
const badgeText = $("vnc-badge-text");
const overlay = $("vnc-overlay-msg");
const overlayText = $("vnc-overlay-text");
const overlaySub = $("vnc-overlay-sub");
const overlayBtn = $("vnc-overlay-btn");
const spinner = $("vnc-spinner");
const scaleBtn = $("vnc-btn-scale");
const scaleText = $("vnc-scale-text");
const reconnectBtn = $("vnc-btn-reconnect");
const fullscreenBtn = $("vnc-btn-fullscreen");

// No password is ever embedded in the page: /vnc/ws authenticates to
// x11vnc server-side and offers the browser only the "None" security
// type, so getPassword() below should never actually be invoked. It
// stays only as a fallback (remembered in memory for this tab only,
// never persisted or sent to the server) in case that ever changes.
let vncPassword = null;

let rfb = null;
let scaleMode = true;
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
      scaleViewport: scaleMode,
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
      onStatus: (state, message, clean) => {
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
        // Deliberately NOT branching on `clean` here. noVNC only ever
        // marks a disconnect unclean (_rfbCleanDisconnect = false) from
        // inside its own _fail(), which exclusively fires during the
        // handshake/connecting phase - a server closing an already-
        // CONNECTED session (x11vnc restarting, the proxy losing its
        // upstream) goes through _socketClose()'s plain 'connected' case
        // instead, which always reports clean=true regardless of the
        // real WebSocket close code or reason. Trusting that flag here
        // meant a mid-session server-side drop (e.g. "restart x11vnc")
        // was read as an intentional disconnect and silently gave up
        // instead of retrying - intentionalDisconnect above is already
        // the reliable signal for every case where *we* chose to stop.
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

// Controls: Scale mode toggle
scaleBtn?.addEventListener("click", () => {
  scaleMode = !scaleMode;
  if (rfb) {
    rfb.scaleViewport = scaleMode;
  }
  scaleBtn.setAttribute("aria-pressed", String(scaleMode));
  if (scaleText) scaleText.textContent = scaleMode ? "Fit Window" : "Actual 1:1";
  scaleBtn.classList.toggle("btn-primary", scaleMode);
  scaleBtn.classList.toggle("btn-secondary", !scaleMode);
});

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

// #vnc-screen's box comes entirely from .vnc-viewport-card's CSS grid
// (1fr row, see vnc.html) - no JS sizing here. noVNC's own RFB instance
// (rfb.scaleViewport, set in connect() below) watches that box with its
// own ResizeObserver and autoscales the real framebuffer into it,
// letterboxed and centered, on every resize/fullscreen-toggle/orientation
// change. Do not reintroduce a competing width/height override here.

// Initial connection
connect();
