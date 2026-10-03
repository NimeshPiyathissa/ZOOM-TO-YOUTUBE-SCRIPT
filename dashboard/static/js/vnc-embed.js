// Shared noVNC connector - used by the Remote GUI page (vnc.js), the
// global remote-desktop overlay (base.js), the Accounts sign-in flow
// (accounts.js) and the interactive preview on /remote (interact.js).
// Connects through this dashboard's own authenticated /vnc/ws proxy.
// noVNC is vendored under /static/vendor/novnc (1.4.0, unmodified) so the
// admin panel loads no third-party script origin at all - the CSP is
// script-src 'self' only.
//
// No VNC password is asked of the viewer: the /vnc/ws proxy authenticates
// to x11vnc server-side (app/vnc_proxy.py + app/vncauth.py) and offers the
// browser the "None" security type, since access is already gated by the
// dashboard session. The password prompt below survives only as a fallback
// for a proxy/server that still presents VNC auth.
import RFB from '/static/vendor/novnc/core/rfb.js';

// Shared backoff schedule for every page that auto-reconnects a VNC
// session (vnc.js, interact.js, accounts.js, base.js's quick-peek
// overlay): 1s, 2s, 4s, 8s, 16s, capped at 20s - retries forever rather
// than giving up, since these panels are meant to come back on their own
// after a transient blip, but backs off so a sustained outage doesn't get
// hammered every couple of seconds.
export function reconnectDelayMs(attempt, { baseMs = 1000, maxMs = 20000 } = {}) {
  return Math.min(baseMs * 2 ** Math.max(0, attempt - 1), maxMs);
}

// Reduce this VNC session's own bandwidth/CPU footprint while
// ffmpeg-stream is actually live, so the preview can't compete with the
// encoder for the same link/CPU - restores the caller's normal setting
// the moment it isn't. Polls the already-cheap /api/state (fixed to run
// off the event loop - see app/main.py's api_state) every 5s rather than
// reacting to a push, since every page that opens a VNC session already
// treats that endpoint as side-effect-free to hit repeatedly.
const LIVE_QUALITY = 1;
const LIVE_COMPRESSION = 6;

function watchLiveBandwidth(rfb, normalQuality) {
  let lastLive = null;
  const tick = async () => {
    let live = false;
    try {
      const res = await fetch("/api/state", { credentials: "same-origin" });
      if (res.ok) {
        const data = await res.json();
        live = data?.stream?.phase === "LIVE";
      }
    } catch (err) { /* keep last known mode on a transient fetch failure */ return; }
    if (live === lastLive) return;
    lastLive = live;
    try {
      rfb.qualityLevel = live ? LIVE_QUALITY : normalQuality;
      rfb.compressionLevel = live ? LIVE_COMPRESSION : 2;
    } catch (err) { /* rfb already torn down */ }
  };
  tick();
  const timer = setInterval(tick, 5000);
  return () => clearInterval(timer);
}

export function promptText(message) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.style.zIndex = "9999";
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="vnc-pass-title">
        <h3 id="vnc-pass-title">VNC password</h3>
        <p>${message}</p>
        <div class="field"><input class="input" id="vnc-pass-input" type="password" autocomplete="off" autofocus></div>
        <div class="btn-row"><button id="vnc-pass-ok" class="btn btn-primary">Connect</button><button id="vnc-pass-cancel" class="btn btn-ghost">Cancel</button></div>
      </div>`;
    document.body.appendChild(backdrop);
    const input = backdrop.querySelector("#vnc-pass-input");
    let done = false;
    const finish = (v) => { if (done) return; done = true; release(); backdrop.remove(); resolve(v); };
    const trap = typeof trapFocus === "function" ? trapFocus : (el, onEsc) => {
      const handler = (e) => { if (e.key === "Escape") onEsc(); };
      window.addEventListener("keydown", handler);
      return () => window.removeEventListener("keydown", handler);
    };
    const release = trap(backdrop, () => finish(null));
    backdrop.querySelector("#vnc-pass-ok").onclick = () => finish(input.value);
    backdrop.querySelector("#vnc-pass-cancel").onclick = () => finish(null);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(input.value); });
    setTimeout(() => input.focus(), 0);
  });
}

/**
 * connectVnc(target, { onDisconnect, onConnect, onStatus, getPassword, password, path, scaleViewport, qualityLevel, showDotCursor })
 *  getPassword: optional async () => string|null. When given, it is
 *  called on 'credentialsrequired' instead of the built-in prompt (so a
 *  caller can remember the password in memory for the page's lifetime);
 *  returning null cancels the connection.
 *  password: optional string to auto-send when credentials are required.
 *  path: WebSocket endpoint path (default "/vnc/ws").
 *  scaleViewport: boolean (default true).
 *  qualityLevel / showDotCursor: optional passthroughs to the RFB instance.
 *  onStatus(state, message): optional - the single place every caller gets
 *  told what's happening ("connecting", "connected", "disconnected" with a
 *  `clean` third arg, or "securityfailure"), so a caller that wants a
 *  status overlay (the
 *  Remote GUI page, the Accounts sign-in panel) can drive it from one
 *  source instead of re-deriving it from raw RFB events - this is what
 *  replaced vnc.js's own parallel copy of this event wiring (see its
 *  header comment / 2026-10-01 fix).
 */
export function connectVnc(target, { onDisconnect, onConnect, onStatus, getPassword, password, path = "/vnc/ws", scaleViewport = true, qualityLevel, showDotCursor } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Request the "binary" subprotocol explicitly. noVNC 1.4 defaults
  // wsProtocols to [] (no subprotocol requested); our /vnc/ws proxy
  // accepts with subprotocol "binary", and a server echoing a
  // subprotocol the client never offered makes the browser abort the
  // handshake with code 1006 - the "VNC disconnected unexpectedly" bug.
  // Asking for "binary" here makes client and proxy agree.
  const wsUrl = `${proto}://${location.host}${path}`;
  if (onStatus) onStatus("connecting", "Connecting to remote desktop…");
  const rfb = new RFB(target, wsUrl, { wsProtocols: ["binary"] });
  rfb.scaleViewport = scaleViewport;
  rfb.resizeSession = false;
  if (qualityLevel !== undefined) rfb.qualityLevel = qualityLevel;
  if (showDotCursor !== undefined) rfb.showDotCursor = showDotCursor;
  rfb.addEventListener("credentialsrequired", async () => {
    let pass = password;
    if (!pass) {
      pass = getPassword ? await getPassword() : await promptText("Enter the VNC password to connect.");
    }
    if (pass == null) { try { rfb.disconnect(); } catch (err) { /* already gone */ } return; }
    rfb.sendCredentials({ password: pass });
  });
  rfb.addEventListener("securityfailure", (e) => {
    const reason = e.detail && e.detail.reason ? e.detail.reason : "VNC authentication failed";
    // Distinct from "disconnected": a caller must not treat a rejected
    // password the same as a dropped connection (that way lies an
    // infinite auto-reconnect loop hammering the same bad password).
    if (onStatus) onStatus("securityfailure", reason);
  });
  let stopBandwidthWatch = null;
  rfb.addEventListener("connect", () => {
    if (onStatus) onStatus("connected", "Connected");
    if (onConnect) onConnect();
    stopBandwidthWatch = watchLiveBandwidth(rfb, qualityLevel !== undefined ? qualityLevel : 6);
  });
  rfb.addEventListener("disconnect", (e) => {
    if (stopBandwidthWatch) { stopBandwidthWatch(); stopBandwidthWatch = null; }
    const clean = e.detail && e.detail.clean;
    if (!clean && typeof toast === "function") toast("VNC disconnected unexpectedly", "err");
    if (onStatus) onStatus("disconnected", clean ? "Disconnected" : "Connection lost unexpectedly", clean);
    if (onDisconnect) onDisconnect(clean);
  });
  return rfb;
}
