// /remote - the touch-interactive preview ("Interact").
//
// One input channel: this drives the *existing* x11vnc session through the
// same authenticated /vnc/ws proxy the Remote GUI page uses, so there is no
// second (xdotool) pointer path fighting noVNC over focus. noVNC does the
// transport, framebuffer decoding, desktop mouse/trackpad input and
// keyboard; this file adds what noVNC's core lacks for a phone:
//
//  * a touch layer (capture phase, so noVNC's own gesture handler never
//    sees a finger): tap = left click, long-press = right click, drag =
//    click-and-drag, two-finger drag = wheel, pinch = *local* zoom of the
//    preview (noVNC's built-in pinch would send Ctrl+wheel to the remote
//    and zoom the web page for viewers - exactly what we don't want);
//  * local zoom implemented by resizing/offsetting noVNC's container, not
//    with a CSS transform: noVNC maps client coordinates via the canvas's
//    bounding rect and its own scale, so a transform would silently skew
//    every tap. Resizing keeps its math exact under scaling, letterboxing
//    and zoom alike;
//  * an on-screen keyboard (real keysyms via rfb.sendKey, one-shot
//    modifiers, F-keys) and a type/paste field for long strings;
//  * quick-action buttons over the preview for the fiddly targets.
//
// Safety: off by default, never persisted (a reload is always passive),
// confirmation when the stream is LIVE, auto-off when the tab is hidden.
// The x11vnc poll rate is raised by the server only while a VNC session
// exists (app/vnc_proxy.py), so a dead page can never leave it fast.
import { connectVnc, promptText } from '/static/js/vnc-embed.js';

const $ = (id) => document.getElementById(id);
const box = $("panel-preview"), viewport = $("rd-viewport"), stage = $("rd-stage"), img = $("preview-img");
const connectingEl = $("rd-connecting"), badge = $("rd-badge"), quick = $("rd-quick"), statusEl = $("rd-status");
const toggleBtn = $("rd-toggle"), kbdToggle = $("rd-kbd-toggle"), keyboard = $("rd-keyboard"), textInput = $("rd-text");

// X11 keysyms (subset) - see noVNC core/input/keysym.js for the full table.
const KS = {
  BackSpace: 0xff08, Tab: 0xff09, Return: 0xff0d, Escape: 0xff1b, Home: 0xff50, Left: 0xff51, Up: 0xff52,
  Right: 0xff53, Down: 0xff54, Page_Up: 0xff55, Page_Down: 0xff56, End: 0xff57, Delete: 0xffff, space: 0x20,
  Shift_L: 0xffe1, Control_L: 0xffe3, Alt_L: 0xffe9, Super_L: 0xffeb, v: 0x76,
};
for (let i = 1; i <= 12; i++) KS["F" + i] = 0xffbe + i - 1;
const CODE = {
  BackSpace: "Backspace", Tab: "Tab", Return: "Enter", Escape: "Escape", Home: "Home", Left: "ArrowLeft", Up: "ArrowUp",
  Right: "ArrowRight", Down: "ArrowDown", Page_Up: "PageUp", Page_Down: "PageDown", End: "End", Delete: "Delete", space: "Space",
};
const MODS = { ctrl: [KS.Control_L, "ControlLeft"], alt: [KS.Alt_L, "AltLeft"], shift: [KS.Shift_L, "ShiftLeft"], super: [KS.Super_L, "MetaLeft"] };

const state = {
  active: false, connected: false, rfb: null,
  zoom: 1, pan: { x: 0, y: 0 },
  streamPhase: "STOPPED",
  vncPassword: null,      // memory only, for this page's lifetime
  mods: new Set(),        // one-shot sticky modifiers
};
window.zsInteract = { get active() { return state.active; } };
window.addEventListener("zsdash:state", (e) => { const p = e.detail && e.detail.stream && e.detail.stream.phase; if (p) state.streamPhase = p; });

const isLive = () => state.streamPhase === "LIVE" || state.streamPhase === "RECONNECTING";
const canvas = () => stage.querySelector("canvas");
const post = (url, body) => apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined });
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const vibrate = (ms) => { try { navigator.vibrate && navigator.vibrate(ms); } catch (err) { /* optional */ } };

// ---------------------------------------------------------------- local zoom (stage geometry)

const MAX_ZOOM = 4;
function layoutStage() {
  const vw = viewport.clientWidth, vh = viewport.clientHeight;
  const sw = vw * state.zoom, sh = vh * state.zoom;
  state.pan.x = clamp(state.pan.x, vw - sw, 0);
  state.pan.y = clamp(state.pan.y, vh - sh, 0);
  stage.style.width = sw + "px"; stage.style.height = sh + "px";
  stage.style.left = state.pan.x + "px"; stage.style.top = state.pan.y + "px";
  const zoomed = state.zoom > 1.01;
  $("rd-zoom-reset").hidden = !zoomed;
  $("rd-zoom-label").textContent = state.zoom.toFixed(1).replace(/\.0$/, "") + "×";
  box.classList.toggle("is-zoomed", zoomed);
}
// Keep the content point that was under `anchorClient` (at zoom0/pan0)
// under `targetClient` at the new zoom - this is both "zoom around the
// fingers" and "pan while pinching" in one formula.
function zoomTo(newZoom, anchorClient, targetClient, zoom0, pan0) {
  const r = viewport.getBoundingClientRect();
  const ax = anchorClient.x - r.left, ay = anchorClient.y - r.top;
  const tx = targetClient.x - r.left, ty = targetClient.y - r.top;
  const ux = (ax - pan0.x) / zoom0, uy = (ay - pan0.y) / zoom0;   // content coords (viewport px at zoom 1)
  state.zoom = clamp(newZoom, 1, MAX_ZOOM);
  state.pan = { x: tx - ux * state.zoom, y: ty - uy * state.zoom };
  layoutStage();
}
function resetZoom() { state.zoom = 1; state.pan = { x: 0, y: 0 }; layoutStage(); }
window.addEventListener("resize", () => { if (state.active) layoutStage(); });

// ---------------------------------------------------------------- synthetic input into noVNC

function mouse(type, x, y, button, buttons) {
  const c = canvas(); if (!c) return;
  // `buttons` bitmask: left=1, right=2, middle=4 (differs from `button` numbering)
  const mask = buttons != null ? buttons : (type === "mouseup" ? 0 : button === 2 ? 2 : button === 1 ? 4 : 1);
  c.dispatchEvent(new MouseEvent(type, { clientX: x, clientY: y, button: button || 0, buttons: mask, bubbles: true, cancelable: true, view: window }));
  if (type === "mouseup") {
    // noVNC emulates pointer capture on mousedown with window-level
    // listeners it only releases on a *real* window mouseup; give it one
    // so a later desktop-mouse move can't be misrouted to the canvas.
    window.dispatchEvent(new MouseEvent("mouseup", { clientX: x, clientY: y, button: button || 0, bubbles: false, view: window }));
  }
}
function click(button, x, y) {
  mouse("mousemove", x, y, 0, 0);          // hover first: Zoom's toolbar reveals on hover
  mouse("mousedown", x, y, button);
  mouse("mouseup", x, y, button);
}
function wheel(x, y, dx, dy) {
  const c = canvas(); if (!c) return;
  c.dispatchEvent(new WheelEvent("wheel", { clientX: x, clientY: y, deltaX: dx, deltaY: dy, deltaMode: 0, bubbles: true, cancelable: true, view: window }));
}

// ---------------------------------------------------------------- touch gesture layer

const SLOP = 10, LONG_MS = 550, PINCH_SLOP = 25, SCROLL_STEP = 28, WHEEL_STEP = 50;
let touch = null;      // single-finger gesture in progress
let multi = null;      // two-finger gesture in progress
let ignoreUntilAllUp = false;
const dist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
const mid = (a, b) => ({ x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 });

function onTouchStart(e) {
  e.preventDefault(); e.stopPropagation();
  if (!state.connected || ignoreUntilAllUp) return;
  if (e.touches.length === 1) {
    const t = e.touches[0];
    touch = { id: t.identifier, x0: t.clientX, y0: t.clientY, x: t.clientX, y: t.clientY, mode: "pending", timer: null };
    touch.timer = setTimeout(() => {
      if (touch && touch.mode === "pending") { touch.mode = "long"; click(2, touch.x, touch.y); vibrate(25); }
    }, LONG_MS);
  } else if (e.touches.length >= 2) {
    if (touch) { clearTimeout(touch.timer); if (touch.mode === "drag") mouse("mouseup", touch.x, touch.y, 0); touch = null; }
    const [a, b] = e.touches, c = mid(a, b);
    multi = { mode: null, d0: dist(a, b), c0: c, last: c, zoom0: state.zoom, pan0: { ...state.pan }, accX: 0, accY: 0 };
  }
}
function onTouchMove(e) {
  e.preventDefault(); e.stopPropagation();
  if (multi && e.touches.length >= 2) {
    const [a, b] = e.touches, d = dist(a, b), c = mid(a, b);
    if (!multi.mode) {
      if (Math.abs(d - multi.d0) > PINCH_SLOP) multi.mode = "pinch";
      else if (Math.hypot(c.x - multi.c0.x, c.y - multi.c0.y) > SLOP) multi.mode = "scroll";
    }
    if (multi.mode === "pinch") {
      zoomTo(multi.zoom0 * d / multi.d0, multi.c0, c, multi.zoom0, multi.pan0);
    } else if (multi.mode === "scroll") {
      // Natural direction: fingers move up -> content scrolls down (positive deltaY)
      multi.accX += c.x - multi.last.x; multi.accY += c.y - multi.last.y;
      while (Math.abs(multi.accY) >= SCROLL_STEP) { wheel(c.x, c.y, 0, multi.accY > 0 ? -WHEEL_STEP : WHEEL_STEP); multi.accY -= Math.sign(multi.accY) * SCROLL_STEP; }
      while (Math.abs(multi.accX) >= SCROLL_STEP) { wheel(c.x, c.y, multi.accX > 0 ? -WHEEL_STEP : WHEEL_STEP, 0); multi.accX -= Math.sign(multi.accX) * SCROLL_STEP; }
    }
    multi.last = c;
    return;
  }
  if (!touch) return;
  const t = Array.from(e.touches).find((x) => x.identifier === touch.id); if (!t) return;
  touch.x = t.clientX; touch.y = t.clientY;
  if (touch.mode === "pending" && Math.hypot(touch.x - touch.x0, touch.y - touch.y0) > SLOP) {
    clearTimeout(touch.timer); touch.mode = "drag";
    mouse("mousemove", touch.x0, touch.y0, 0, 0);
    mouse("mousedown", touch.x0, touch.y0, 0);
  }
  if (touch.mode === "drag") mouse("mousemove", touch.x, touch.y, 0, 1);
}
function onTouchEnd(e) {
  e.preventDefault(); e.stopPropagation();
  if (multi) {
    if (e.touches.length < 2) { multi = null; ignoreUntilAllUp = e.touches.length > 0; }
    return;
  }
  if (touch) {
    clearTimeout(touch.timer);
    if (touch.mode === "pending" && e.type === "touchend") click(0, touch.x, touch.y);
    else if (touch.mode === "drag") mouse("mouseup", touch.x, touch.y, 0);
    touch = null;
  }
  if (e.touches.length === 0) ignoreUntilAllUp = false;
}
// Capture phase: these run before noVNC's canvas listeners and stop the
// event there, so noVNC's GestureHandler never sees a touch at all.
viewport.addEventListener("touchstart", onTouchStart, { capture: true, passive: false });
viewport.addEventListener("touchmove", onTouchMove, { capture: true, passive: false });
viewport.addEventListener("touchend", onTouchEnd, { capture: true, passive: false });
viewport.addEventListener("touchcancel", onTouchEnd, { capture: true, passive: false });

// Desktop: mouse/trackpad go to noVNC natively (it already handles
// buttons, drag, wheel scroll). Only Ctrl+wheel / trackpad pinch (which
// browsers report as Ctrl+wheel) is intercepted for local zoom, and the
// context menu is kept off the page.
viewport.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault(); e.stopPropagation();
  const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  const p = { x: e.clientX, y: e.clientY };
  zoomTo(state.zoom * factor, p, p, state.zoom, { ...state.pan });
}, { capture: true, passive: false });
viewport.addEventListener("contextmenu", (e) => e.preventDefault());
$("rd-zoom-reset").addEventListener("click", resetZoom);

// ---------------------------------------------------------------- Interact on / off

async function getPassword() {
  if (state.vncPassword) return state.vncPassword;
  const p = await promptText("Enter the VNC password to control the display. It is kept in memory for this page only.");
  if (p) state.vncPassword = p;
  return p;
}

function setStatus(text) { statusEl.textContent = text; }

async function turnOn() {
  if (state.active) return;
  if (isLive() && !(await confirmDialog(
      "Enable direct control while LIVE? Every tap, drag and keypress on the preview happens on the real display and is visible to viewers.",
      { danger: true, confirmText: "Enable" }))) return;
  state.active = true; state.connected = false;
  resetZoom();
  box.classList.add("is-interactive");
  viewport.hidden = false; connectingEl.hidden = false; img.hidden = true; $("preview-placeholder").hidden = true;
  badge.hidden = false; quick.hidden = false;
  toggleBtn.setAttribute("aria-pressed", "true"); toggleBtn.classList.replace("btn-secondary", "btn-primary");
  kbdToggle.disabled = false;
  setStatus("Interactive · connecting…");
  try {
    const rfb = connectVnc(stage, { getPassword, onDisconnect: (clean) => { if (state.active) turnOff(clean ? "disconnected" : "connection lost"); } });
    state.rfb = rfb;
    rfb.qualityLevel = 5;         // lighter JPEG for x11vnc to encode at the higher poll rate
    rfb.showDotCursor = true;     // always see where a tap will land
    rfb.addEventListener("connect", () => {
      state.connected = true; connectingEl.hidden = true;
      setStatus("Interactive · live (~60 fps) · tap = click · hold = right-click · 2 fingers = scroll · pinch = zoom");
      layoutStage();
      try { rfb.focus({ preventScroll: true }); } catch (err) { /* older noVNC */ }
      // Ask x11vnc to poll fast while we're actually interacting. Client-
      // driven (not tied to the proxy connection) so it maps exactly to
      // "Interact is on" and can't get stuck at the slow rate on reconnect.
      post("/api/vnc/rate", { mode: "fast" }).catch(() => {});
      announce("Interactive preview connected");
    });
    rfb.addEventListener("securityfailure", (e) => {
      state.vncPassword = null;
      toast("VNC password rejected" + (e.detail && e.detail.reason ? ": " + e.detail.reason : ""), "err");
    });
  } catch (err) {
    toast("Could not start the interactive preview: " + err.message, "err");
    turnOff("failed");
  }
}

function turnOff(reason) {
  const rfb = state.rfb; state.rfb = null;
  state.active = false; state.connected = false;
  if (rfb) { try { rfb.disconnect(); } catch (err) { /* already gone */ } }
  stage.innerHTML = "";
  resetZoom();
  box.classList.remove("is-interactive", "is-zoomed");
  viewport.hidden = true; connectingEl.hidden = true;
  badge.hidden = true; quick.hidden = true;
  toggleBtn.setAttribute("aria-pressed", "false"); toggleBtn.classList.replace("btn-primary", "btn-secondary");
  kbdToggle.disabled = true; kbdToggle.setAttribute("aria-pressed", "false"); keyboard.hidden = true;
  state.mods.clear(); document.querySelectorAll(".rd-mod.is-on").forEach((b) => b.classList.remove("is-on"));
  setStatus("Passive preview · 1 frame / 3 s" + (reason && reason !== "user" ? " · Interact off (" + reason + ")" : ""));
  // Drop x11vnc back to the low idle poll now that we're done interacting.
  post("/api/vnc/rate", { mode: "slow" }).catch(() => {});
  if (typeof pollPreview === "function") pollPreview();   // bring the thumbnail back right away
}

toggleBtn.addEventListener("click", () => (state.active ? turnOff("user") : turnOn()));
// Best-effort: if the page is torn down mid-session, still ask for slow so
// x11vnc doesn't keep polling fast (keepalive lets the request outlive the page).
window.addEventListener("pagehide", () => {
  if (!state.active) return;
  try {
    fetch("/api/vnc/rate", { method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": (typeof csrfToken === "function" ? csrfToken() : "") },
      body: JSON.stringify({ mode: "slow" }) });
  } catch (err) { /* leaving anyway */ }
});

// ---------------------------------------------------------------- preview fullscreen (fills the phone)
// Distinct from the YouTube "Full" quick action (which fullscreens the
// *player on the remote* for viewers): this expands the *preview element*
// on THIS device, so the video / interactive canvas fills the phone. Works
// in both passive and interactive mode.
const fsBtn = $("rd-fs-toggle");
function previewFsElement() { return document.fullscreenElement || document.webkitFullscreenElement; }
async function togglePreviewFullscreen() {
  try {
    if (previewFsElement()) {
      await (document.exitFullscreen ? document.exitFullscreen() : document.webkitExitFullscreen());
    } else {
      await (box.requestFullscreen ? box.requestFullscreen({ navigationUI: "hide" }) : box.webkitRequestFullscreen());
    }
  } catch (err) { toast("Fullscreen not available: " + err.message, "err"); }
}
fsBtn.addEventListener("click", togglePreviewFullscreen);
function onFsChange() {
  const on = previewFsElement() === box;
  box.classList.toggle("is-fullscreen", on);
  fsBtn.setAttribute("aria-pressed", String(on));
  fsBtn.classList.toggle("btn-primary", on); fsBtn.classList.toggle("btn-secondary", !on);
  // The preview box just changed size - keep noVNC's geometry exact.
  if (state.active) layoutStage();
}
document.addEventListener("fullscreenchange", onFsChange);
document.addEventListener("webkitfullscreenchange", onFsChange);

document.addEventListener("visibilitychange", () => { if (document.hidden && state.active) turnOff("tab hidden"); });
window.addEventListener("pagehide", () => { if (state.rfb) { try { state.rfb.disconnect(); } catch (err) { /* leaving */ } } });

// ---------------------------------------------------------------- keyboard drawer

kbdToggle.addEventListener("click", () => {
  const open = keyboard.hidden;
  keyboard.hidden = !open; kbdToggle.setAttribute("aria-pressed", String(open));
  if (open) keyboard.scrollIntoView({ block: "nearest", behavior: "smooth" });
});

function sendKey(keysym, code, down) { if (state.rfb && state.connected) state.rfb.sendKey(keysym, code, down); }
function withMods(fn) {
  const mods = Array.from(state.mods);
  mods.forEach((m) => sendKey(MODS[m][0], MODS[m][1], true));
  fn();
  mods.reverse().forEach((m) => sendKey(MODS[m][0], MODS[m][1], false));
  state.mods.clear(); document.querySelectorAll(".rd-mod.is-on").forEach((b) => b.classList.remove("is-on"));
}
$("rd-keys").addEventListener("click", (e) => {
  const btn = e.target.closest(".rd-key"); if (!btn) return;
  if (btn.dataset.mod) {
    const m = btn.dataset.mod;
    if (state.mods.has(m)) { state.mods.delete(m); btn.classList.remove("is-on"); } else { state.mods.add(m); btn.classList.add("is-on"); }
    return;
  }
  const name = btn.dataset.key;
  withMods(() => { sendKey(KS[name], CODE[name] || name, true); sendKey(KS[name], CODE[name] || name, false); });
  vibrate(10);
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function typeText(text, thenEnter) {
  if (!state.connected) { toast("Turn Interact on first", "err"); return; }
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    const keysym = ch === "\n" ? KS.Return : ch === "\t" ? KS.Tab : (cp < 0x100 ? cp : 0x01000000 | cp);
    sendKey(keysym, null, true); sendKey(keysym, null, false);
    await sleep(12);
  }
  if (thenEnter) { sendKey(KS.Return, "Enter", true); sendKey(KS.Return, "Enter", false); }
}
$("rd-type").addEventListener("click", async (e) => { const t = textInput.value; if (!t) return; await withLoading(e.currentTarget, () => typeText(t, false)); textInput.value = ""; });
$("rd-type-enter").addEventListener("click", async (e) => { const t = textInput.value; if (!t) return; await withLoading(e.currentTarget, () => typeText(t, true)); textInput.value = ""; });
textInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("rd-type-enter").click(); } });
$("rd-paste").addEventListener("click", async () => {
  const t = textInput.value; if (!t || !state.connected) return;
  // Remote clipboard (x11vnc puts it on the X selection), then Ctrl+V into
  // the focused window - robust for very long strings.
  state.rfb.clipboardPasteFrom(t);
  await sleep(150);
  sendKey(KS.Control_L, "ControlLeft", true); sendKey(KS.v, "KeyV", true); sendKey(KS.v, "KeyV", false); sendKey(KS.Control_L, "ControlLeft", false);
  toast("Pasted into the focused window");
});

// ---------------------------------------------------------------- quick actions

const QUICK = {
  "focus-zoom": () => post("/api/window/focus", { which: "zoom" }).then((r) => toast("Focused: " + (r.title || "Zoom"))),
  "open-browser": () => (window.zsOpenBrowser
    ? window.zsOpenBrowser()
    : post("/api/browser/open", {}).then((r) => toast("Browser opened at Google" + (r.signed_in_as ? " · signed in as " + r.signed_in_as : "")))),
  "zoom-join": () => post("/api/zoom/join").then(() => toast("Zoom join sent")),
  "zoom-leave": async () => {
    if (!(await confirmDialog("Leave the Zoom meeting? The stream keeps running on a plain slate.", { danger: true, confirmText: "Leave" }))) return;
    await post("/api/zoom/leave-with-choice", { then: "slate" }); toast("Left the meeting - canvas on slate");
  },
  "zoom-mic": () => post("/api/zoom/control", { action: "mic" }).then((r) => {
    const s = r.verify && r.verify.available ? r.verify.state : "unknown";
    toast(s === "unknown" ? "Alt+A sent - Zoom's mic state couldn't be read back" : "Zoom mic: " + s.replace("_", " "), s === "unknown" ? "err" : "ok");
    if (typeof canvasPoll === "function") setTimeout(canvasPoll, 500);
  }),
  "yt-play": () => post("/api/youtube/control", { action: "play" }).then(() => toast("Play")),
  "yt-pause": () => post("/api/youtube/control", { action: "pause" }).then(() => toast("Pause")),
  "yt-fullscreen": () => post("/api/youtube/control", { action: "fullscreen" }).then(() => toast("Fullscreen toggled")),
  "zoom-reset": () => { resetZoom(); return Promise.resolve(); },
};
quick.addEventListener("click", async (e) => {
  const btn = e.target.closest(".rd-qbtn"); if (!btn) return;
  const fn = QUICK[btn.dataset.q]; if (!fn) return;
  await withLoading(btn, async () => { try { await fn(); } catch (err) { toast(err.message, "err"); } });
});
