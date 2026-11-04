// /edumix - EduMix Live (mjphysics.lk).
//
// The operator signs in on the REAL site: the page below is a live noVNC
// view of the isolated Chrome profile (same authenticated /vnc/ws proxy the
// Remote GUI uses). Phone number and OTP are typed by the operator into that
// view - directly, or through the sign-in helper, which sends keystrokes to
// the browser and wipes the field. Nothing typed there is posted to the
// dashboard, stored, or logged.
//
// Status comes from GET /api/edumix/status (page-state classifier in
// app/edumix.py); polling backs off exponentially while the dashboard is
// unreachable. Capture / YouTube output are disabled server-side and shown
// as such - this page never starts an encoder.
import { connectVnc, reconnectDelayMs, CLOSE_AUTH_FAILED } from '/static/js/vnc-embed.js';

const $ = (id) => document.getElementById(id);
const post = (url, body) => apiFetch(url, { method: "POST", body: JSON.stringify(body || {}) });
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const STATE_ICON = {
  ok: "check-circle-2", info: "info", warn: "alert-triangle", bad: "alert-circle", idle: "circle-help",
};

let status = null;
let prevState = null;
let rfb = null;
let vncState = "connecting";      // connecting | connected | down | authfail
let vncAttempt = 0;
let vncTimer = null;
let fitMode = "fit";
let pollFailures = 0;
let helperShownForEpisode = false;
let busy = false;
let pausedHidden = false;       // tab hidden: view dropped on purpose, no auto-reconnect

// ------------------------------------------------------------------ helpers

function setText(id, text) { const el = $(id); if (el && el.textContent !== text) el.textContent = text; }
function setLevel(id, level) { const el = $(id); if (el) el.dataset.level = level; }
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function nowClock() { return new Date().toLocaleTimeString([], { hour12: false }); }

function pushEvent(text, level = "info") {
  const list = $("em-events");
  list.querySelector(".em-event-empty")?.remove();
  const li = document.createElement("li");
  li.dataset.level = level;
  const t = document.createElement("time"); t.textContent = nowClock();
  const s = document.createElement("span"); s.textContent = text;
  li.append(t, s);
  list.prepend(li);
  while (list.children.length > 8) list.lastElementChild.remove();
}

// ------------------------------------------------------------------ live view (noVNC)

function desktopAspect() {
  const c = $("em-screen").querySelector("canvas");
  return c && c.width > 0 && c.height > 0 ? c.width / c.height : 16 / 9;
}

// Fit = the whole desktop, letterboxed (aspect ratio kept). Fill = scaled to
// cover the frame, centred, overflow cropped (aspect ratio still kept - never
// stretched). noVNC maps pointer coordinates through its own scale, and we
// only resize its container, so clicks stay exact in both modes.
function applyFit() {
  const wrap = $("em-stage-wrap"), stage = $("em-stage");
  stage.dataset.fit = fitMode;
  if (fitMode === "fill") {
    const cw = wrap.clientWidth, ch = wrap.clientHeight, ar = desktopAspect();
    let w = cw, h = cw / ar;
    if (h < ch) { h = ch; w = ch * ar; }
    Object.assign(stage.style, { width: `${Math.ceil(w)}px`, height: `${Math.ceil(h)}px`, left: `${Math.round((cw - w) / 2)}px`, top: `${Math.round((ch - h) / 2)}px` });
  } else {
    Object.assign(stage.style, { width: "100%", height: "100%", left: "0px", top: "0px" });
  }
  document.querySelectorAll("#em-fit-seg .seg-btn").forEach((b) => {
    const on = b.dataset.fit === fitMode;
    b.classList.toggle("is-active", on); b.setAttribute("aria-pressed", String(on));
  });
  window.dispatchEvent(new Event("resize"));   // noVNC re-fits its canvas to the new container
}

function connectView() {
  clearTimeout(vncTimer);
  try { rfb?.disconnect(); } catch (e) { /* already gone */ }
  $("em-screen").replaceChildren();
  vncState = "connecting"; renderOverlay();
  rfb = connectVnc($("em-screen"), {
    scaleViewport: true,
    onStatus: (s) => {
      if (s === "connected") { vncState = "connected"; vncAttempt = 0; applyFit(); pushEvent("Live view connected", "ok"); }
      else if (s === "securityfailure") { vncState = "authfail"; }
      renderChips(); renderOverlay();
    },
    onDisconnect: (_clean, code) => {
      if (pausedHidden) { vncState = "down"; return; }
      if (code === CLOSE_AUTH_FAILED || vncState === "authfail") { vncState = "authfail"; renderChips(); renderOverlay(); return; }
      vncState = "down"; renderChips(); renderOverlay();
      vncAttempt += 1;
      const delay = reconnectDelayMs(vncAttempt);
      if (vncAttempt === 1) pushEvent("Live view lost - reconnecting", "warn");
      vncTimer = setTimeout(connectView, delay);
    },
  });
}

// ------------------------------------------------------------------ rendering

function renderStatus() {
  if (!status) return;
  const lvl = status.level || "idle";
  setLevel("em-status", lvl);
  $("em-status-icon").innerHTML = icon(STATE_ICON[lvl] || "info");
  setText("em-status-label", status.label || status.state);
  setText("em-status-hint", status.hint || "");
  const rc = status.reconnect;
  if (rc && rc.attempt && ["browser_down", "network_down", "player_error", "error"].includes(status.state)) {
    setText("em-status-hint", rc.gave_up
      ? `${status.hint} Automatic retries stopped - use Reconnect.`
      : `${status.hint} Retry ${rc.attempt}${rc.next_in ? ` in ${Math.ceil(rc.next_in)}s` : ""}.`);
  }
  renderChips();
  renderControls();
  renderOverlay();
}

function renderChips() {
  const s = status || {};
  const view = { connected: ["ok", "connected"], connecting: ["info", "connecting"], down: ["bad", "reconnecting"], authfail: ["bad", "auth failed"] }[vncState];
  setLevel("em-chip-view", view[0]); setText("em-chip-view-t", view[1]);

  const st = s.state;
  let sess = ["idle", "unknown"];
  if (["login_required", "awaiting_otp"].includes(st)) sess = ["warn", "sign-in needed"];
  else if (st === "session_expired") sess = ["warn", "expired"];
  else if (["authenticated", "class_open", "no_live", "player_loading", "buffering", "playing", "paused", "ended"].includes(st)) sess = ["ok", `valid · ${ago(s.session_last_ok)}`];
  else if (s.session_last_ok) sess = ["idle", `last valid ${ago(s.session_last_ok)}`];
  setLevel("em-chip-session", sess[0]); setText("em-chip-session-t", sess[1]);

  const p = s.player || {};
  let pl = ["idle", "none"];
  if (st === "protected") pl = ["bad", "protected"];
  else if (st === "player_error") pl = ["bad", "error"];
  else if (st === "playing") pl = ["ok", p.width ? `playing ${p.width}×${p.height}` : "playing"];
  else if (st === "buffering") pl = ["info", "buffering"];
  else if (st === "paused") pl = ["info", "paused"];
  else if (st === "ended") pl = ["info", "ended"];
  else if (st === "player_loading") pl = ["info", "loading"];
  else if (p.present === false && p.frames?.length) pl = ["info", "embedded player"];
  setLevel("em-chip-player", pl[0]); setText("em-chip-player-t", pl[1]);

  let net = ["ok", "online"];
  if (st === "network_down" || !navigator.onLine) net = ["bad", "offline"];
  else if (st === "browser_down") net = ["bad", "browser unreachable"];
  else if (pollFailures) net = ["warn", "dashboard unreachable"];
  else if (!s.active) net = ["idle", "idle"];
  setLevel("em-chip-net", net[0]); setText("em-chip-net-t", net[1]);
}

function renderControls() {
  const active = !!status?.active;
  $("em-login").disabled = !active || busy;
  $("em-reconnect").disabled = !active || busy;
  $("em-stop").disabled = !active || busy;
  $("em-helper").disabled = !active;
  $("em-player-fit").disabled = !active;
  $("em-open").disabled = busy;
  const src = status?.source;
  if (src) $("em-opt-auto").checked = !!src.auto_navigate;
  $("em-capture-badge").title = status?.capture?.reason || "";
}

// The overlay sits over the live view for the states where the view alone
// isn't self-explanatory. Signed-in / playing / sign-in states keep the view
// fully interactive (no overlay).
function renderOverlay() {
  const ov = $("em-overlay");
  const st = status?.state;
  let kind = null, title = "", text = "", actions = [], iconName = "";
  if (vncState === "authfail") {
    kind = "error"; iconName = "lock"; title = "Live view refused the connection";
    text = "The VNC proxy rejected authentication. Check the Remote GUI page; retries are paused.";
    actions = [["Retry", "btn-secondary", connectView]];
  } else if (!status) {
    kind = "loading"; title = "Connecting…"; text = "";
  } else if (!status.active) {
    kind = "idle"; iconName = "globe"; title = "No class open";
    text = "Paste an MJPhysics class URL above and press Open class. If you aren't signed in, the site's own login appears here.";
  } else if (vncState !== "connected") {
    kind = vncState === "down" ? "offline" : "loading";
    iconName = vncState === "down" ? "wifi-off" : "";
    title = vncState === "down" ? "Live view disconnected" : "Connecting to the live view…";
    text = vncState === "down" ? "Reconnecting automatically with backoff." : "";
  } else if (st === "browser_down" || st === "stopped") {
    kind = "offline"; iconName = "wifi-off"; title = status.label; text = status.hint;
    actions = [["Reconnect", "btn-primary", doReconnect]];
  } else if (st === "network_down") {
    kind = "offline"; iconName = "wifi-off"; title = "Network offline"; text = status.hint;
    actions = [["Reconnect now", "btn-primary", doReconnect]];
  } else if (st === "session_expired") {
    kind = "warn"; iconName = "lock-keyhole"; title = "Session expired";
    text = status.hint; actions = [["Re-authenticate", "btn-primary", doLogin]];
  } else if (st === "protected") {
    kind = "error"; iconName = "shield-check"; title = "Protected playback";
    text = status.hint;
  } else if (st === "player_error" || st === "error") {
    kind = "error"; iconName = "alert-circle"; title = status.label; text = status.hint;
    actions = [["Reconnect", "btn-primary", doReconnect]];
  } else if (st === "loading") {
    kind = "loading"; title = "Loading the page…";
  } else if (st === "no_live") {
    kind = "note"; iconName = "radio"; title = "No live class right now";
    text = "The site lists none. This view updates by itself when one starts.";
  }
  ov.dataset.kind = kind || "none";
  ov.hidden = !kind;
  if (!kind) return;
  $("em-spinner").hidden = kind !== "loading";
  const ic = $("em-overlay-icon");
  ic.hidden = !iconName; ic.innerHTML = iconName ? icon(iconName) : "";
  setText("em-overlay-title", title);
  setText("em-overlay-text", text);
  const box = $("em-overlay-actions");
  box.replaceChildren();
  for (const [label, cls, fn] of actions) {
    const b = document.createElement("button");
    b.className = `btn ${cls}`; b.type = "button"; b.textContent = label; b.onclick = fn;
    box.append(b);
  }
}

function onTransition(prev, cur) {
  if (prev === cur) return;
  const lvl = status.level;
  if (prev !== null) pushEvent(status.label, lvl === "bad" ? "bad" : lvl === "warn" ? "warn" : lvl === "ok" ? "ok" : "info");
  if (cur === "session_expired") toast("EduMix session expired - re-authenticate", "warn");
  else if (cur === "protected") toast("Protected playback - capture is not available", "err");
  else if (cur === "player_error") toast("The player reported an error", "err");
  else if (cur === "network_down") toast("Network offline - reconnecting", "warn");
  else if (cur === "browser_down" && prev !== null) toast("EduMix browser unreachable - recovering", "warn");
  else if (prev && ["login_required", "awaiting_otp"].includes(prev) && ["authenticated", "class_open", "no_live", "playing"].includes(cur)) toast("Signed in to EduMix", "ok");
  if (["login_required", "awaiting_otp"].includes(cur)) {
    if (!helperShownForEpisode) { helperShownForEpisode = true; openHelper(); }
  } else if (cur !== "browser_down" && cur !== "loading") {
    helperShownForEpisode = false;
    if (!$("em-dialog").hidden && ["authenticated", "class_open", "no_live", "playing"].includes(cur)) closeHelper();
  }
}

// ------------------------------------------------------------------ polling

async function poll() {
  try {
    status = await apiFetch("/api/edumix/status");
    pollFailures = 0;
    onTransition(prevState, status.state);
    prevState = status.state;
    renderStatus();
    setTimeout(poll, status.active ? 2500 : 6000);
  } catch (err) {
    pollFailures += 1;
    if (pollFailures === 1) pushEvent("Dashboard unreachable - retrying", "warn");
    renderChips();
    setTimeout(poll, reconnectDelayMs(pollFailures, { baseMs: 2000, maxMs: 30000 }));
  }
}

// ------------------------------------------------------------------ actions

async function run(btn, fn) {
  if (busy) return;
  busy = true; renderControls();
  try { await (btn ? withLoading(btn, fn) : fn()); }
  catch (err) { toast(err.message, "err"); pushEvent(err.message, "bad"); }
  finally { busy = false; renderControls(); }
}

function showUrlError(msg) {
  const el = $("em-url-error");
  el.textContent = msg || ""; el.hidden = !msg;
  $("em-url").setAttribute("aria-invalid", msg ? "true" : "false");
}

function looksLikeEduMix(raw) {
  try {
    const u = new URL(/^[a-z]+:\/\//i.test(raw) ? raw : `https://${raw}`);
    const h = u.hostname.toLowerCase();
    return u.protocol === "https:" && (h === "mjphysics.lk" || h.endsWith(".mjphysics.lk"));
  } catch (e) { return false; }
}

async function doOpen(e) {
  e?.preventDefault();
  const raw = $("em-url").value.trim();
  if (!raw) return showUrlError("Paste an MJPhysics class URL.");
  if (!looksLikeEduMix(raw)) return showUrlError("Only https://mjphysics.lk links are accepted.");
  showUrlError("");
  await run($("em-open"), async () => {
    const res = await post("/api/edumix/open", { url: raw, auto_navigate: $("em-opt-auto").checked });
    pushEvent(res.hot_swapped ? "Navigating to the class" : "Starting the EduMix browser", "info");
    helperShownForEpisode = false;
    await refreshNow();
  });
}
async function doLogin() {
  await run($("em-login"), async () => { await post("/api/edumix/login"); pushEvent("Opened the site's sign-in", "info"); helperShownForEpisode = false; await refreshNow(); });
}
async function doReconnect() {
  await run($("em-reconnect"), async () => { const r = await post("/api/edumix/reconnect"); pushEvent(r.mode === "restart_browser" ? "Restarting the browser" : "Reconnecting to the class", "info"); await refreshNow(); });
}
async function doStop() {
  if (!(await confirmDialog("Stop the EduMix browser? Your saved session stays in its isolated profile.", { confirmText: "Stop", danger: true }))) return;
  await run($("em-stop"), async () => { await post("/api/edumix/stop"); pushEvent("EduMix browser stopped", "info"); await refreshNow(); });
}
async function refreshNow() {
  try { status = await apiFetch("/api/edumix/status"); prevState = status.state; renderStatus(); } catch (e) { /* poll loop reports it */ }
}

async function applyPlayerFit(mode) {
  try {
    const r = await post("/api/edumix/fit", { mode });
    if (r.applied) toast(`Player ${mode === "reset" ? "fit reset" : "set to " + mode} (${r.videos} video${r.videos === 1 ? "" : "s"})`, "ok");
    else toast(r.note || "No video on the page itself", "warn");
  } catch (err) { toast(err.message, "warn"); }
}

// ------------------------------------------------------------------ sign-in helper

const KS = { Tab: 0xff09, Return: 0xff0d };
const keysym = (ch) => { const cp = ch.codePointAt(0); return cp < 0x100 ? cp : 0x01000000 + cp; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function openHelper() {
  const st = status?.state;
  $("em-dialog-title").textContent = st === "awaiting_otp" ? "Enter the OTP" : "Sign in on the site";
  $("em-dialog-text").textContent = st === "awaiting_otp"
    ? "Click the code field in the live view, then type the code you received here and send it."
    : "Click the phone-number field in the live view, then type your number here and send it - or type straight into the view.";
  $("em-secret-label").textContent = st === "awaiting_otp" ? "One-time code" : "Phone number";
  $("em-secret").setAttribute("inputmode", "numeric");
  const d = $("em-dialog"); d.hidden = false;
  d._release = trapFocus(d, closeHelper);
  $("em-secret").focus();
}
function closeHelper() {
  const d = $("em-dialog");
  $("em-secret").value = "";
  d._release?.(); d._release = null;
  d.hidden = true;
}
async function sendSecret(extra) {
  const field = $("em-secret");
  const text = field.value;
  field.value = "";                                   // wipe immediately - never retained
  if (!rfb || vncState !== "connected") return toast("Live view isn't connected", "warn");
  for (const ch of text) { rfb.sendKey(keysym(ch)); await sleep(18); }
  if (extra) rfb.sendKey(extra);
  field.focus();
}

// ------------------------------------------------------------------ wiring

$("em-form").addEventListener("submit", doOpen);
$("em-login").addEventListener("click", doLogin);
$("em-reconnect").addEventListener("click", doReconnect);
$("em-stop").addEventListener("click", doStop);
$("em-helper").addEventListener("click", openHelper);
$("em-dialog-close").addEventListener("click", closeHelper);
$("em-send").addEventListener("click", () => sendSecret(null));
$("em-send-enter").addEventListener("click", () => sendSecret(KS.Return));
$("em-send-tab").addEventListener("click", () => { if (rfb && vncState === "connected") rfb.sendKey(KS.Tab); });
$("em-secret").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendSecret(KS.Return); } });
$("em-secret-eye").addEventListener("click", (e) => {
  const f = $("em-secret"), show = f.type === "password";
  f.type = show ? "text" : "password";
  e.currentTarget.setAttribute("aria-pressed", String(show));
});
$("em-dialog").addEventListener("click", (e) => { if (e.target === $("em-dialog")) closeHelper(); });
$("em-url").addEventListener("input", () => showUrlError(""));
$("em-opt-auto").addEventListener("change", async (e) => {
  if (!status?.source) return;
  try { await post("/api/edumix/options", { auto_navigate: e.target.checked }); } catch (err) { toast(err.message, "err"); }
});

document.querySelectorAll("#em-fit-seg .seg-btn").forEach((b) => b.addEventListener("click", () => { fitMode = b.dataset.fit; applyFit(); }));
$("em-fit-reset").addEventListener("click", () => { fitMode = "fit"; applyFit(); if (status?.active) applyPlayerFit("reset"); });
$("em-player-fit").addEventListener("click", () => applyPlayerFit(fitMode === "fill" ? "fill" : "fit"));
$("em-fullscreen").addEventListener("click", async () => {
  const wrap = $("em-stage-wrap");
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await wrap.requestFullscreen();
  } catch (err) { toast("Fullscreen isn't available here", "warn"); }
});
document.addEventListener("fullscreenchange", () => { $("em-fullscreen").setAttribute("aria-pressed", String(!!document.fullscreenElement)); applyFit(); });
new ResizeObserver(() => { if (fitMode === "fill") applyFit(); }).observe($("em-stage-wrap"));
window.addEventListener("online", () => { pushEvent("Browser back online", "ok"); renderChips(); });
window.addEventListener("offline", () => { pushEvent("This device went offline", "warn"); renderChips(); });
// Don't hold a VNC session (and its x11vnc poll rate) for a hidden tab.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { pausedHidden = true; clearTimeout(vncTimer); try { rfb?.disconnect(); } catch (e) { /* gone */ } }
  else if (pausedHidden) { pausedHidden = false; vncAttempt = 0; connectView(); }
});
window.addEventListener("beforeunload", () => { try { rfb?.disconnect(); } catch (e) { /* gone */ } });

renderOverlay();
connectView();
poll();
