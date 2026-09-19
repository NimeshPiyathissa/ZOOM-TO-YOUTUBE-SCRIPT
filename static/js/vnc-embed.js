// Shared noVNC connector - used by the Remote GUI page (vnc.js), the
// global remote-desktop overlay (base.js), the Accounts sign-in flow
// (accounts.js) and the interactive preview on /remote (interact.js).
// Connects through this dashboard's own authenticated /vnc/ws proxy.
// noVNC is vendored under /static/vendor/novnc (1.4.0, unmodified) so the
// admin panel loads no third-party script origin at all - the CSP is
// script-src 'self' only.
//
// The separate VNC password is asked for once per page load (interact.js
// keeps it in a JS variable for the life of the page - never in
// localStorage/sessionStorage, never sent anywhere but the RFB handshake).
import RFB from '/static/vendor/novnc/core/rfb.js';

export function promptText(message) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
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
    const release = trapFocus(backdrop, () => finish(null));
    backdrop.querySelector("#vnc-pass-ok").onclick = () => finish(input.value);
    backdrop.querySelector("#vnc-pass-cancel").onclick = () => finish(null);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(input.value); });
    setTimeout(() => input.focus(), 0);
  });
}

/**
 * connectVnc(target, { onDisconnect, getPassword })
 *  getPassword: optional async () => string|null. When given, it is
 *  called on 'credentialsrequired' instead of the built-in prompt (so a
 *  caller can remember the password in memory for the page's lifetime);
 *  returning null cancels the connection.
 */
export function connectVnc(target, { onDisconnect, getPassword } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const rfb = new RFB(target, `${proto}://${location.host}/vnc/ws`);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.addEventListener("credentialsrequired", async () => {
    const password = getPassword ? await getPassword() : await promptText("Enter the VNC password to connect.");
    if (password == null) { try { rfb.disconnect(); } catch (err) { /* already gone */ } return; }
    rfb.sendCredentials({ password });
  });
  rfb.addEventListener("disconnect", (e) => {
    const clean = e.detail && e.detail.clean;
    if (!clean) toast("VNC disconnected unexpectedly", "err");
    if (onDisconnect) onDisconnect(clean);
  });
  return rfb;
}
