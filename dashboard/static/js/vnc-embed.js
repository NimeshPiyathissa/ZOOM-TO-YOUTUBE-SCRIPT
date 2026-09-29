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
 * connectVnc(target, { onDisconnect, onConnect, getPassword, password, path, scaleViewport })
 *  getPassword: optional async () => string|null. When given, it is
 *  called on 'credentialsrequired' instead of the built-in prompt (so a
 *  caller can remember the password in memory for the page's lifetime);
 *  returning null cancels the connection.
 *  password: optional string to auto-send when credentials are required.
 *  path: WebSocket endpoint path (default "/vnc/ws").
 *  scaleViewport: boolean (default true).
 */
export function connectVnc(target, { onDisconnect, onConnect, getPassword, password, path = "/vnc/ws", scaleViewport = true } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Request the "binary" subprotocol explicitly. noVNC 1.4 defaults
  // wsProtocols to [] (no subprotocol requested); our /vnc/ws proxy
  // accepts with subprotocol "binary", and a server echoing a
  // subprotocol the client never offered makes the browser abort the
  // handshake with code 1006 - the "VNC disconnected unexpectedly" bug.
  // Asking for "binary" here makes client and proxy agree.
  const wsUrl = `${proto}://${location.host}${path}`;
  const rfb = new RFB(target, wsUrl, { wsProtocols: ["binary"] });
  rfb.scaleViewport = scaleViewport;
  rfb.resizeSession = false;
  rfb.addEventListener("credentialsrequired", async () => {
    let pass = password;
    if (!pass) {
      pass = getPassword ? await getPassword() : await promptText("Enter the VNC password to connect.");
    }
    if (pass == null) { try { rfb.disconnect(); } catch (err) { /* already gone */ } return; }
    rfb.sendCredentials({ password: pass });
  });
  if (onConnect) {
    rfb.addEventListener("connect", onConnect);
  }
  rfb.addEventListener("disconnect", (e) => {
    const clean = e.detail && e.detail.clean;
    if (!clean && typeof toast === "function") toast("VNC disconnected unexpectedly", "err");
    if (onDisconnect) onDisconnect(clean);
  });
  return rfb;
}
