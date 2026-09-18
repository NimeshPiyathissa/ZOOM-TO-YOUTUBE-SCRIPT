// Shared noVNC connector - used by the Remote GUI page (vnc.js) and
// embedded inside the Accounts sign-in flow (accounts.js). Connects
// through this dashboard's own authenticated /vnc/ws proxy; the separate
// VNC password is asked for once per connection and never stored here.
import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@1.4.0/core/rfb.js';

function promptText(message) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="vnc-pass-title">
        <h3 id="vnc-pass-title">VNC password</h3>
        <p>${message}</p>
        <div class="field"><input class="input" id="vnc-pass-input" type="password" autofocus></div>
        <div class="btn-row"><button id="vnc-pass-ok" class="btn btn-primary">Connect</button></div>
      </div>`;
    document.body.appendChild(backdrop);
    const release = trapFocus(backdrop, () => submit());
    const input = backdrop.querySelector("#vnc-pass-input");
    const submit = () => { const v = input.value; release(); backdrop.remove(); resolve(v); };
    backdrop.querySelector("#vnc-pass-ok").onclick = submit;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  });
}

export function connectVnc(target, { onDisconnect } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const rfb = new RFB(target, `${proto}://${location.host}/vnc/ws`);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.addEventListener("credentialsrequired", async () => {
    const password = await promptText("Enter the VNC password to connect.");
    rfb.sendCredentials({ password });
  });
  rfb.addEventListener("disconnect", (e) => {
    const clean = e.detail && e.detail.clean;
    toast("VNC disconnected" + (clean ? "" : " unexpectedly"), clean ? "ok" : "err");
    if (onDisconnect) onDisconnect(clean);
  });
  return rfb;
}
