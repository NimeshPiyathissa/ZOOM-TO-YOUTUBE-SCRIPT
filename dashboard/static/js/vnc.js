// Remote GUI (/vnc) client using noVNC with auto-reconnect, fit-to-window scaling,
// and automated VNC authentication.
import RFB from '/static/vendor/novnc/core/rfb.js';
import { promptText } from '/static/js/vnc-embed.js';

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

const vncConfig = JSON.parse($("vnc-config")?.textContent || "{}");
let vncPassword = vncConfig.password || null;

let rfb = null;
let scaleMode = true;
let reconnectTimer = null;
let intentionalDisconnect = false;
let isConnected = false;

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

  intentionalDisconnect = false;
  setStatus("connecting");

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${proto}://${location.host}/vnc/ws`;

  try {
    rfb = new RFB(target, wsUrl, { wsProtocols: ["binary"] });
    rfb.scaleViewport = scaleMode;
    rfb.resizeSession = false;
    rfb.qualityLevel = 6;
    rfb.showDotCursor = true;

    rfb.addEventListener("connect", () => {
      isConnected = true;
      setStatus("connected");
      try { rfb.focus({ preventScroll: true }); } catch (err) { /* older noVNC */ }
      post("/api/vnc/rate", { mode: "fast" }).catch(() => {});
      if (typeof announce === "function") announce("Remote GUI connected");
    });

    rfb.addEventListener("credentialsrequired", async () => {
      if (vncPassword) {
        rfb.sendCredentials({ password: vncPassword });
      } else {
        const pass = await promptText("Enter the VNC password to connect to display :99.");
        if (pass == null) {
          intentionalDisconnect = true;
          try { rfb.disconnect(); } catch (err) {}
          setStatus("disconnected", "Password cancelled", "Enter password to connect");
          return;
        }
        vncPassword = pass;
        rfb.sendCredentials({ password: pass });
      }
    });

    rfb.addEventListener("securityfailure", (e) => {
      vncPassword = null;
      const reason = e.detail && e.detail.reason ? `: ${e.detail.reason}` : "";
      if (typeof toast === "function") toast("VNC authentication failed" + reason, "err");
      intentionalDisconnect = true;
      setStatus("disconnected", "Authentication failed", "VNC password was rejected. Check settings.");
    });

    rfb.addEventListener("disconnect", (e) => {
      const wasConnected = isConnected;
      isConnected = false;
      const clean = e.detail && e.detail.clean;

      if (intentionalDisconnect || clean) {
        setStatus("disconnected");
      } else {
        // Auto-reconnect after 2 seconds
        setStatus("reconnecting", "Reconnecting in 2s…", wasConnected ? "Connection lost unexpectedly" : "Could not establish initial connection");
        reconnectTimer = setTimeout(() => {
          connect();
        }, 2000);
      }
    });

  } catch (err) {
    setStatus("disconnected", "Failed to start viewer", err.message);
  }
}

// Controls: Reconnect
reconnectBtn?.addEventListener("click", () => {
  intentionalDisconnect = false;
  connect();
});
overlayBtn?.addEventListener("click", () => {
  intentionalDisconnect = false;
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

// Keep #vnc-screen locked to the bot's actual 16:9 framebuffer
// (RESOLUTION=1920x1080) instead of whatever non-16:9 box
// .vnc-viewport-card's layout happens to produce at a given window size.
// noVNC's own autoscale() measures this element's child, so giving it an
// exact 16:9 pixel box up front means the scaled canvas always fills it
// with no edge cut off - no CSS aspect-ratio/flexbox ambiguity involved.
// Reset to CSS's 100%/100% before each measurement so this can't drift
// by measuring its own previous output on repeated calls (e.g. rapid
// resize events).
function fitStage169() {
  target.style.width = "";
  target.style.height = "";
  const bw = target.clientWidth, bh = target.clientHeight;
  if (!bw || !bh) return;
  let w = bw, h = Math.round(bw * 9 / 16);
  if (h > bh) { h = bh; w = Math.round(bh * 16 / 9); }
  target.style.width = `${w}px`;
  target.style.height = `${h}px`;
}
fitStage169();
// Observe the viewport card, not target itself - target's own size
// changes as a *result* of fitStage169(), so observing it would
// re-trigger the callback on every programmatic resize.
if (typeof ResizeObserver !== "undefined" && viewport) {
  new ResizeObserver(fitStage169).observe(viewport);
}
window.addEventListener("resize", fitStage169);

// Initial connection
connect();
