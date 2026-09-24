// /zoom - every way into a Zoom meeting, the live meeting state, and the
// saved-meetings library.
//
// The library is NOT its own store: it is the zoom-type rows of the one
// `sources` table that /remote's Sources tiles, /controls and /schedule
// also read. Every write goes through /api/sources, the server bumps a
// revision, and this page (like /remote) polls GET /api/sources?since=rev
// every 3 s and re-renders only when it changed - so an edit here shows
// up on /remote within a few seconds and vice versa, with no manual
// refresh and no second copy of the data anywhere.
//
// Secrets: the page only ever holds the public view (pwd=/tk= redacted,
// passcode masked). "Reveal" fetches them for one card, once, audited.
// Status is whatever zoom-status.py could read from Zoom's windows and
// accessibility tree - labelled as such, never assumed.
import { connectVnc } from '/static/js/vnc-embed.js';

const $ = (id) => document.getElementById(id);
const post = (url, body) => apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let meetings = JSON.parse($("zm-data-meetings").textContent) || [];
let activeId = JSON.parse($("zm-data-active").textContent);
let rev = JSON.parse($("zm-data-rev").textContent) || 0;
let streamPhase = "STOPPED";
let lastStatus = null;
let editingId = null;      // library id being edited, or null for "add"
let parsed = null;         // last /api/zoom/parse result (raw url/passcode never rendered)
let revealedSecrets = null;
let zoomRunning = false;   // zoom.service up - "Join" then means a restart of the client

const active = () => meetings.find((m) => m.id === activeId) || null;
const isLive = () => streamPhase === "LIVE" || streamPhase === "RECONNECTING";

// ---------------------------------------------------------------- status vocabulary

const STATUS = {
  not_joined:            { label: "Not joined",          icon: "circle-slash",   tone: "idle" },
  connecting:            { label: "Connecting",          icon: "hourglass",      tone: "wait" },
  waiting_room:          { label: "In the waiting room", icon: "hourglass",      tone: "wait",
                           guide: "The host has to admit the bot. Nothing here can do that.", actions: ["screen"] },
  not_started:           { label: "Host hasn't started", icon: "clock",          tone: "wait",
                           guide: "Zoom joins by itself when the host starts. If it doesn't, rejoin.", actions: ["rejoin"] },
  in_meeting:            { label: "In the meeting",      icon: "circle-check",   tone: "ok" },
  ended:                 { label: "Meeting ended",       icon: "circle-slash",   tone: "idle",
                           guide: "The meeting or webinar is over. Reset the window so the canvas is clean.", actions: ["reset", "rejoin"] },
  expired:               { label: "Link expired",        icon: "alert-triangle", tone: "bad",
                           guide: "Zoom error 3038: the event is over or not opened yet. Reset the window; rejoin when the host's event is live, or edit the meeting with a fresh link.", actions: ["reset", "edit"] },
  join_failed:           { label: "Couldn't join",       icon: "alert-triangle", tone: "bad",
                           guide: "Zoom refused the join. Check the link and passcode, dismiss the dialog, then rejoin.", actions: ["dismiss", "edit", "rejoin"] },
  passcode_required:     { label: "Passcode needed",     icon: "lock-keyhole",   tone: "bad",
                           guide: "Zoom is asking for a passcode, or rejected the saved one. Set it on this meeting and rejoin.", actions: ["passcode", "rejoin"] },
  registration_required: { label: "Registration required", icon: "clipboard-list", tone: "bad",
                           guide: "This webinar only admits registrants. Register on the remote screen, then save the personal link Zoom issues.", actions: ["register", "edit"] },
  removed:               { label: "Removed by the host", icon: "user-x",         tone: "bad",
                           guide: "The host removed the bot. Ask to be re-admitted, then rejoin.", actions: ["rejoin"] },
  locked:                { label: "Meeting locked",      icon: "lock-keyhole",   tone: "wait",
                           guide: "The host locked the meeting. Ask them to unlock it, then rejoin.", actions: ["rejoin"] },
  signin_required:       { label: "Sign-in required",    icon: "log-in",         tone: "bad",
                           guide: "Only signed-in Zoom users are admitted. Set this meeting to join with a Google account, sign in on the remote screen, then rejoin.", actions: ["edit", "screen"] },
  duplicate_join:        { label: "Link already used",  icon: "alert-triangle", tone: "bad",
                           guide: "This registrant link has already been used to join - it's single-use per registrant. Re-register for a fresh one.", actions: ["edit"] },
  wrong_registrant:      { label: "Wrong registrant",   icon: "user-x",         tone: "bad",
                           guide: "Zoom doesn't recognize this session as the registrant this link was issued for. Check the Gmail address on file, or re-register.", actions: ["edit"] },
  unknown:               { label: "Unknown",             icon: "circle-help",    tone: "idle",
                           guide: "Zoom is running but nothing recognizable is on screen - look at the remote screen.", actions: ["screen"] },
};
const JOIN_VIA_LABEL = { client: "Desktop client", web: "Web client" };
const KIND = { meeting: "Meeting", webinar: "Webinar", pmi: "Personal room" };
const INPUT_KIND = {
  join_link: ["link-2", "Join link"], personal_link: ["ticket", "Personal registrant link"], registration: ["clipboard-list", "Registration page"],
  meeting_id: ["hash", "Meeting ID"], vanity: ["house", "Personal room"], deep_link: ["link", "zoommtg:// link"], invite: ["mail", "Email invite"],
};

// ---------------------------------------------------------------- meeting status

let statusTimer = null;
async function pollStatus() {
  if (document.hidden) return;
  let st;
  try { st = await apiFetch("/api/zoom/status"); } catch (err) { return; }
  lastStatus = st;
  renderStatus(st);
}

function renderStatus(st) {
  const key = STATUS[st.status] ? st.status : "unknown";
  const v = STATUS[key];
  const box = $("zm-status");
  const changed = box.dataset.status !== key;
  box.dataset.status = key; box.dataset.tone = v.tone;
  $("zm-status-icon").innerHTML = icon(v.icon);
  $("zm-status-label").textContent = v.label;
  $("zm-status-detail").textContent = st.detail || "";
  if (changed) { box.classList.remove("is-changed"); void box.offsetWidth; box.classList.add("is-changed"); announce("Zoom: " + v.label); }
  // producer service (zoom.service for the desktop client, browser-source.service for the web client)
  const svc = st.service || "unknown";
  const svcUnit = st.join_via === "web" ? "browser-source.service" : "zoom.service";
  $("zm-service-badge").className = "badge " + ({ LIVE: "badge-active", STARTING: "badge-activating", FAILED: "badge-failed" }[svc] || "badge-inactive");
  $("zm-service-text").textContent = svcUnit + " " + svc.toLowerCase();
  const viaBadge = $("zm-via-badge");
  if (st.join_via && key !== "not_joined") { viaBadge.hidden = false; $("zm-via-text").textContent = "via " + (JOIN_VIA_LABEL[st.join_via] || st.join_via); }
  else viaBadge.hidden = true;
  // guidance + one-tap actions
  const g = $("zm-guidance");
  const a = active();
  const acts = (v.actions || []).filter((x) => !(x === "register" && !(a && a.link_kind === "registration")));
  if (v.guide) {
    g.hidden = false; g.className = "banner zm-guidance tone-" + v.tone;
    $("zm-guidance-icon").innerHTML = icon(v.icon);
    $("zm-guidance-text").textContent = v.guide;
    $("zm-guidance-quote").textContent = (st.detail && /Zoom says/.test(st.detail)) ? "" : (st.dialogs && st.dialogs.length ? "Dialog on screen: " + st.dialogs.join(" · ") : "");
    $("zm-guidance-actions").innerHTML = acts.map((k) => ACTION_BTN[k]).join("");
  } else { g.hidden = true; }
  $("zm-dismiss").hidden = !(st.dialogs && st.dialogs.length);
  // buttons
  const running = svc === "LIVE" || svc === "STARTING";
  zoomRunning = running;
  const inMeeting = key === "in_meeting";
  $("zm-join").hidden = running && key !== "not_joined";
  $("zm-rejoin").hidden = !running;
  $("zm-leave-slate").hidden = !running; $("zm-leave-stop").hidden = !running;
  $("zm-join").disabled = !a; $("zm-sticky-join").disabled = !a;
  $("zm-sticky-join").hidden = running && key !== "not_joined"; $("zm-sticky-leave").hidden = !running;
  $("zm-sticky-status").textContent = v.label;
  // in-meeting controls
  $("zm-controls").classList.toggle("is-off", !inMeeting);
  $("zm-controls-note").textContent = inMeeting ? "state read back from Zoom's toolbar" : "available once in a meeting";
  paintCtl("mic", st.mic, { muted: "Muted", unmuted: "LIVE", no_audio: "No audio", unknown: "Unknown" }, { muted: "mic-off", unmuted: "mic", no_audio: "mic-off", unknown: "mic" });
  paintCtl("camera", st.camera, { on: "On", off: "Off", unknown: "Unknown" }, { on: "video", off: "video-off", unknown: "video" });
}

function paintCtl(kind, v, labels, icons) {
  const state = (v && v.available && v.state) || "unknown";
  const btn = $("zm-" + kind);
  btn.dataset.state = state;
  $(`zm-${kind}-state`).textContent = labels[state] || "Unknown";
  $(`zm-${kind}-icon`).innerHTML = icon(icons[state] || icons.unknown);
  if (state === "unknown" || state === "no_audio") btn.removeAttribute("aria-pressed");
  else btn.setAttribute("aria-pressed", String(state === "unmuted" || state === "on"));
  btn.title = v && v.reason ? v.reason : (v && v.name ? `Zoom shows "${v.name}"` : "");
}

const ACTION_BTN = {
  rejoin:   `<button class="btn btn-primary btn-sm" data-act="rejoin">${icon("rotate-cw", "icon-sm")}Rejoin</button>`,
  reset:    `<button class="btn btn-secondary btn-sm" data-act="reset">${icon("refresh-ccw-dot", "icon-sm")}Reset Zoom window</button>`,
  dismiss:  `<button class="btn btn-secondary btn-sm" data-act="dismiss">${icon("x", "icon-sm")}Dismiss dialog</button>`,
  edit:     `<button class="btn btn-secondary btn-sm" data-act="edit">${icon("pencil", "icon-sm")}Edit this meeting</button>`,
  passcode: `<button class="btn btn-secondary btn-sm" data-act="passcode">${icon("lock-keyhole", "icon-sm")}Set passcode</button>`,
  register: `<button class="btn btn-secondary btn-sm" data-act="register">${icon("clipboard-list", "icon-sm")}Open registration</button>`,
  screen:   `<button class="btn btn-secondary btn-sm" data-act="screen">${icon("monitor", "icon-sm")}Show remote screen</button>`,
};
$("zm-guidance-actions").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const a = active();
  const run = {
    rejoin: () => zoomAction("rejoin"), reset: resetWindow, dismiss: dismissDialog,
    edit: () => a && startEdit(a.id), passcode: () => { if (a) { startEdit(a.id); setTimeout(() => $("zm-passcode").focus(), 50); } },
    register: () => a && openRegistration(a.id), screen: () => { vncConnect(); $("zm-vnc").scrollIntoView({ behavior: "smooth", block: "center" }); },
  }[b.dataset.act];
  if (run) await withLoading(b, run);
});

// ---------------------------------------------------------------- join / leave / reset

async function zoomAction(action) {
  try {
    const r = await post("/api/zoom/" + action);
    toast({ join: "Joining…", rejoin: "Rejoining…", leave: "Left the meeting" }[action] + (r.ok === false ? " (" + (r.stderr || "error") + ")" : ""));
    setTimeout(pollStatus, 1500); setTimeout(pollJoinOptions, 6000);
  } catch (err) { toast(err.message, "err"); }
}
$("zm-join").addEventListener("click", (e) => withLoading(e.currentTarget, () => joinActive()));
$("zm-sticky-join").addEventListener("click", (e) => withLoading(e.currentTarget, () => joinActive()));
$("zm-rejoin").addEventListener("click", (e) => withLoading(e.currentTarget, () => zoomAction("rejoin")));
async function joinActive() {
  const a = active();
  if (!a) { toast("Pick a meeting in the library first", "err"); return; }
  if (a.missing && a.missing.length) { toast("Can't join yet - missing " + a.missing.join("; "), "err"); return; }
  return zoomAction(zoomRunning ? "rejoin" : "join");
}
async function leave(then, msg) {
  if (!(await confirmDialog(msg, { confirmText: then === "stop" ? "Leave and stop" : "Leave", danger: then === "stop" }))) return;
  try { await post("/api/zoom/leave-with-choice", { then }); toast(then === "stop" ? "Left the meeting - stream stopped" : "Left the meeting - canvas on slate"); setTimeout(pollStatus, 1500); }
  catch (err) { toast(err.message, "err"); }
}
$("zm-leave-slate").addEventListener("click", (e) => withLoading(e.currentTarget, () => leave("slate", "Leave the meeting? The stream keeps running on a plain slate.")));
$("zm-leave-stop").addEventListener("click", (e) => withLoading(e.currentTarget, () => leave("stop", "Leave the meeting AND stop the live stream?")));
$("zm-sticky-leave").addEventListener("click", (e) => withLoading(e.currentTarget, () => leave("slate", "Leave the meeting? The stream keeps running on a plain slate.")));

async function resetWindow() {
  if (isLive() && !(await confirmDialog("Reset the Zoom window while LIVE? Dialogs are dismissed first; if Zoom is still stuck it is quit and viewers see the slate. The encoder keeps running.", { confirmText: "Reset" }))) return;
  try {
    const r = await post("/api/zoom/reset");
    toast(r.stopped ? `Dismissed ${r.dismissed.length} dialog(s); Zoom was still stuck, so it was quit to the slate` : `Dismissed ${r.dismissed.length} dialog(s) - Zoom is ${STATUS[r.status_after_dismiss] ? STATUS[r.status_after_dismiss].label.toLowerCase() : r.status_after_dismiss}`);
    setTimeout(pollStatus, 1200);
  } catch (err) { toast(err.message, "err"); }
}
$("zm-reset").addEventListener("click", (e) => withLoading(e.currentTarget, resetWindow));
async function dismissDialog() {
  try { const r = await post("/api/zoom/dialog", { action: "dismiss" }); toast(r.dismissed.length ? "Dismissed: " + r.dismissed.map((d) => d.title).join(", ") : "Nothing to dismiss"); setTimeout(pollStatus, 800); }
  catch (err) { toast(err.message, "err"); }
}
$("zm-dismiss").addEventListener("click", (e) => withLoading(e.currentTarget, dismissDialog));

// ---------------------------------------------------------------- in-meeting controls (read-back, never assumed)

async function control(action, btn) {
  await withLoading(btn, async () => {
    try {
      const r = await post("/api/zoom/control", { action });
      const v = r.verify || {};
      if (action === "mic") paintCtl("mic", v, { muted: "Muted", unmuted: "LIVE", no_audio: "No audio", unknown: "Unknown" }, { muted: "mic-off", unmuted: "mic", no_audio: "mic-off", unknown: "mic" });
      if (action === "camera") paintCtl("camera", v, { on: "On", off: "Off", unknown: "Unknown" }, { on: "video", off: "video-off", unknown: "video" });
      const s = v.available ? v.state : "unknown";
      toast(s === "unknown" ? `Sent to Zoom - state couldn't be read back` : `Zoom reports ${action}: ${s.replace("_", " ")}`, s === "unknown" ? "err" : "ok");
    } catch (err) { toast(err.message, "err"); }
  });
}
$("zm-mic").addEventListener("click", (e) => control("mic", e.currentTarget));
$("zm-camera").addEventListener("click", (e) => control("camera", e.currentTarget));
initSegmented($("zm-view-seg"), (v) => control(v === "speaker" ? "view-speaker" : "view-gallery", $("zm-view-seg")));

async function pollJoinOptions() {
  let r; try { r = await apiFetch("/api/zoom/join-options"); } catch (err) { return; }
  if (!r || !r.state) return;
  const out = $("zm-apply-result");
  if (r.state === "waiting") { out.textContent = "waiting for the meeting window to apply the saved options…"; setTimeout(pollJoinOptions, 5000); }
  else if (r.state === "done") out.textContent = describeApply(r.report);
  else if (r.state === "timeout") out.textContent = "no meeting window appeared in time - options not applied";
  else out.textContent = "";
}
function describeApply(rep) {
  if (!rep) return "";
  const bits = [];
  for (const k of ["mic", "camera"]) {
    const e = rep[k]; if (!e) continue;
    bits.push(`${k}: ${e.available ? (e.before === e.after ? `already ${e.after}` : `${e.before} → ${e.after}`) : "unknown (" + (e.note || "unreadable") + ")"}`);
  }
  if (rep.view) bits.push(`view: ${rep.view.sent ? rep.view.want + " sent" : "not sent"}`);
  return bits.join(" · ");
}
$("zm-apply-options").addEventListener("click", (e) => withLoading(e.currentTarget, async () => {
  try { const rep = await post("/api/zoom/apply-options"); $("zm-apply-result").textContent = rep.reason ? rep.reason : describeApply(rep); toast(rep.reason || "Options applied where Zoom's state could be read"); setTimeout(pollStatus, 800); }
  catch (err) { toast(err.message, "err"); }
}));

// ---------------------------------------------------------------- remote screen (noVNC through the authed proxy)

let rfb = null;
function vncConnect() {
  if (rfb) return;
  $("zm-vnc-idle").hidden = true; $("zm-vnc-status").textContent = "connecting…";
  rfb = connectVnc($("zm-vnc-stage"), { onDisconnect: () => { rfb = null; $("zm-vnc-idle").hidden = false; $("zm-vnc-status").textContent = "not connected"; $("zm-vnc-disconnect").hidden = true; $("zm-vnc-fullscreen").hidden = true; post("/api/vnc/rate", { mode: "slow" }).catch(() => {}); } });
  rfb.addEventListener("connect", () => { $("zm-vnc-status").textContent = "connected to :99"; $("zm-vnc-disconnect").hidden = false; $("zm-vnc-fullscreen").hidden = false; post("/api/vnc/rate", { mode: "fast" }).catch(() => {}); });
}
$("zm-vnc-connect").addEventListener("click", vncConnect);
$("zm-vnc-disconnect").addEventListener("click", () => { try { rfb && rfb.disconnect(); } catch (err) { /* gone */ } });
$("zm-vnc-fullscreen").addEventListener("click", () => { const el = $("zm-vnc"); (document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen && el.requestFullscreen()); });
document.addEventListener("visibilitychange", () => { if (document.hidden && rfb) { try { rfb.disconnect(); } catch (err) { /* gone */ } } });
window.addEventListener("pagehide", () => { if (rfb) navigator.sendBeacon && fetch("/api/vnc/rate", { method: "POST", credentials: "same-origin", keepalive: true, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() }, body: JSON.stringify({ mode: "slow" }) }).catch(() => {}); });

// ---------------------------------------------------------------- editor: smart paste

let parseTimer = null;
$("zm-paste").addEventListener("input", () => { clearTimeout(parseTimer); parseTimer = setTimeout(parseNow, 500); });
$("zm-paste").addEventListener("paste", () => { clearTimeout(parseTimer); parseTimer = setTimeout(parseNow, 80); });
$("zm-parse").addEventListener("click", (e) => withLoading(e.currentTarget, parseNow));
$("zm-passcode").addEventListener("change", () => { if (parsed) refreshPassStatus(); });

async function parseNow() {
  const text = $("zm-paste").value.trim();
  if (!text) { parsed = null; $("zm-parsed").hidden = true; return; }
  if (/•••/.test(text)) { // the redacted URL of a meeting being edited - nothing new to parse
    $("zm-parsed").hidden = true; return;
  }
  try {
    parsed = await post("/api/zoom/parse", { text, passcode: $("zm-passcode").value });
  } catch (err) { toast(err.message, "err"); return; }
  renderParsed();
}

function renderParsed() {
  const p = parsed; const box = $("zm-parsed"); box.hidden = false;
  const ik = INPUT_KIND[p.input_kind] || ["circle-help", "Unrecognized"];
  $("zm-parsed-input").innerHTML = icon(ik[0], "icon-sm") + esc(ik[1]);
  $("zm-parsed-kind").innerHTML = p.meeting_kind ? icon(p.meeting_kind === "webinar" ? "ticket" : p.meeting_kind === "pmi" ? "house" : "video", "icon-sm") + esc(KIND[p.meeting_kind]) : "";
  $("zm-parsed-url").textContent = p.url_redacted || "";
  $("zm-mid").value = p.meeting_id_formatted || "";
  setSeg($("zm-kind-seg"), p.meeting_kind || "meeting");
  refreshPassStatus();
  const notes = [...(p.errors || []).map((t) => ["bad", "alert-triangle", t]), ...(p.warnings || []).map((t) => ["warn", "info", t])];
  $("zm-parsed-notes").innerHTML = notes.map(([tone, ic, t]) => `<li class="tone-${tone}">${icon(ic, "icon-sm")}<span>${esc(t)}</span></li>`).join("");
  if (!$("zm-name").value && p.meeting_id_formatted) $("zm-name").placeholder = (KIND[p.meeting_kind] || "Meeting") + " " + p.meeting_id_formatted;
  if (p.bot_display_name && $("zm-bot-name").value === "Stream Bot") $("zm-bot-name").value = p.bot_display_name;
  box.classList.toggle("is-error", !p.ok);
}
function refreshPassStatus() {
  const p = parsed; const el = $("zm-pass-status");
  const typed = $("zm-passcode").value.trim();
  const inLink = p && p.url_redacted && /pwd=/.test(p.url_redacted);
  if (inLink) el.innerHTML = icon("circle-check", "icon-sm") + "<span>in the link</span>";
  else if (typed || (p && p.has_passcode)) el.innerHTML = icon("circle-check", "icon-sm") + "<span>entered separately</span>";
  else el.innerHTML = icon("circle-help", "icon-sm") + "<span>none - add one if the meeting needs it</span>";
  el.dataset.ok = String(!!(inLink || typed || (p && p.has_passcode)));
}
$("zm-mid").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 11);
  e.target.value = d.length === 11 ? `${d.slice(0, 3)} ${d.slice(3, 7)} ${d.slice(7)}` : d.length > 6 ? `${d.slice(0, 3)} ${d.slice(3, 6)} ${d.slice(6)}` : d.length > 3 ? `${d.slice(0, 3)} ${d.slice(3)}` : d;
});
function setSeg(seg, value) { seg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.value === value)); }
function segValue(seg) { const b = seg.querySelector(".seg-btn.is-active"); return b ? b.dataset.value : null; }
initSegmented($("zm-kind-seg"));
initSegmented($("zm-form-view-seg"));
initSegmented($("zm-signin-seg"), (v) => {
  $("zm-account").hidden = v !== "google";
  $("zm-account-hint").hidden = v !== "google";
  if (v === "google" && !$("zm-account").value) {
    const valid = Array.from($("zm-account").options).find((o) => o.value && !o.disabled);
    if (valid) $("zm-account").value = valid.value;
  }
});
initSegmented($("zm-joinvia-seg"));
$("zm-join-at").addEventListener("input", () => { $("zm-join-at-hint").hidden = !$("zm-join-at").value; });
document.querySelectorAll(".zm-reveal").forEach((b) => b.addEventListener("click", () => {
  const inp = $(b.dataset.for); const show = inp.type === "password"; inp.type = show ? "text" : "password";
  b.setAttribute("aria-pressed", String(show)); b.setAttribute("aria-label", show ? "Hide passcode" : "Show passcode"); b.innerHTML = icon(show ? "eye-off" : "eye");
}));

// ---------------------------------------------------------------- editor: save

function readForm() {
  const signin = segValue($("zm-signin-seg")) || "guest";
  let acctId = signin === "google" ? ($("zm-account").value || null) : null;
  if (signin === "google" && !acctId) {
    const valid = Array.from($("zm-account").options).find((o) => o.value && !o.disabled);
    if (valid) {
      acctId = valid.value;
      $("zm-account").value = valid.value;
    }
  }
  const joinAtRaw = $("zm-join-at").value;
  const opts = {
    bot_name: $("zm-bot-name").value.trim() || "Stream Bot",
    signin_mode: signin,
    meeting_kind: segValue($("zm-kind-seg")) || (parsed && parsed.meeting_kind) || "meeting",
    audio_on: $("zm-audio-on").checked, video_on: $("zm-video-on").checked,
    view: segValue($("zm-form-view-seg")) || "speaker",
    auto_rejoin: $("zm-auto-rejoin").checked, rejoin_max: Number($("zm-rejoin-max").value) || 5,
    join_at: joinAtRaw ? Math.floor(new Date(joinAtRaw).getTime() / 1000) : null,
    join_method: segValue($("zm-joinvia-seg")) || "client",
  };
  const pc = $("zm-passcode").value.trim();
  if (pc) opts.passcode = pc; else if (!editingId) opts.passcode = (parsed && parsed.passcode) || "";
  // Same "blank on edit means keep the saved value" convention as
  // passcode above - the field only ever shows a masked placeholder on
  // edit (registrant_email_masked), never the real address, so there's
  // nothing meaningful to prefill and compare against.
  const regEmail = $("zm-registrant-email").value.trim();
  if (regEmail || !editingId) opts.registrant_email = regEmail;
  if (parsed && parsed.vanity_url) opts.vanity_url = parsed.vanity_url;
  return { name: $("zm-name").value.trim() || $("zm-name").placeholder || "", url: parsed ? parsed.url : "", options: opts,
           account_id: acctId };
}

async function save(thenJoin) {
  const err = $("zm-form-error"); err.hidden = true;
  // A meeting ID edited by hand after detection: re-parse it so the URL matches.
  const typedId = $("zm-mid").value.replace(/\D/g, "");
  if (typedId && (!parsed || parsed.meeting_id !== typedId)) {
    parsed = await post("/api/zoom/parse", { text: typedId, passcode: $("zm-passcode").value });
    renderParsed();
  }
  const body = readForm();
  const missing = [];
  if (!editingId && !(parsed && parsed.ok)) missing.push(parsed && parsed.errors && parsed.errors.length ? parsed.errors[0] : "a Zoom link or meeting ID");
  if (!body.name) missing.push("a name");
  if (body.options.signin_mode === "google" && !body.account_id) missing.push("which Google account to join with (sign in on the Accounts page first, or switch Join as to Guest)");
  if (missing.length) { err.hidden = false; err.textContent = "Can't save yet - missing " + missing.join("; ") + "."; return; }
  body.type = "zoom";
  try {
    let saved;
    if (editingId) saved = (await apiFetch(`/api/sources/${editingId}`, { method: "PUT", body: JSON.stringify(body) })).source;
    else saved = (await post("/api/sources", body)).source;
    toast(editingId ? "Meeting updated" : "Meeting saved");
    await refreshSources(true);
    resetEditor();
    if (thenJoin && saved) await switchTo(saved.id);
  } catch (e2) { err.hidden = false; err.textContent = e2.message; }
}
$("zm-save").addEventListener("click", (e) => withLoading(e.currentTarget, () => save(false)));
$("zm-save-join").addEventListener("click", (e) => withLoading(e.currentTarget, () => save(true)));
$("zm-edit-cancel").addEventListener("click", resetEditor);

function resetEditor() {
  editingId = null; parsed = null; revealedSecrets = null;
  $("zm-editor-title").textContent = "Add a meeting"; $("zm-edit-cancel").hidden = true;
  $("zm-paste").value = ""; $("zm-paste").placeholder = "Join link, personal (tk=) link, registration page, meeting ID, personal room URL, zoommtg:// link - or the whole email";
  $("zm-passcode").value = ""; $("zm-passcode").placeholder = "Passcode (if not in the link)";
  $("zm-parsed").hidden = true; $("zm-name").value = ""; $("zm-name").placeholder = "Monday town hall"; $("zm-bot-name").value = "Stream Bot";
  setSeg($("zm-signin-seg"), "guest"); $("zm-account").hidden = true; $("zm-account-hint").hidden = true; $("zm-account").value = "";
  $("zm-audio-on").checked = false; $("zm-video-on").checked = false; setSeg($("zm-form-view-seg"), "speaker");
  $("zm-auto-rejoin").checked = true; $("zm-rejoin-max").value = 5; $("zm-join-at").value = ""; $("zm-join-at-hint").hidden = true;
  setSeg($("zm-joinvia-seg"), "client"); $("zm-registrant-email").value = ""; $("zm-registrant-email").placeholder = "Only for a per-registrant (tk=) link - leave blank otherwise";
  $("zm-form-error").hidden = true; $("zm-save").innerHTML = icon("check") + "Save meeting";
}

function startEdit(id) {
  const m = meetings.find((x) => x.id === id); if (!m) return;
  resetEditor();
  editingId = id; const o = m.options || {};
  $("zm-editor-title").textContent = "Edit: " + m.name; $("zm-edit-cancel").hidden = false;
  $("zm-paste").value = m.url; $("zm-paste").placeholder = "Paste a new link to replace the saved one";
  $("zm-passcode").placeholder = o.has_passcode ? "saved (leave blank to keep)" : "Passcode (none saved)";
  parsed = { ok: true, input_kind: m.link_kind === "registration" ? "registration" : (m.link_kind === "personal" ? "personal_link" : "join_link"), meeting_kind: o.meeting_kind || "meeting",
             meeting_id: m.meeting_id || "", meeting_id_formatted: m.meeting_id_formatted || "", url: "", url_redacted: m.url, has_passcode: !!o.has_passcode, warnings: [], errors: [] };
  renderParsed();
  $("zm-name").value = m.name; $("zm-bot-name").value = o.bot_name || "Stream Bot";
  setSeg($("zm-signin-seg"), o.signin_mode || "guest"); $("zm-account").hidden = o.signin_mode !== "google"; $("zm-account-hint").hidden = o.signin_mode !== "google"; $("zm-account").value = m.account_id || "";
  if (o.signin_mode === "google" && !$("zm-account").value) {
    const valid = Array.from($("zm-account").options).find((x) => x.value && !x.disabled);
    if (valid) $("zm-account").value = valid.value;
  }
  $("zm-audio-on").checked = !!o.audio_on; $("zm-video-on").checked = !!o.video_on; setSeg($("zm-form-view-seg"), o.view || "speaker");
  $("zm-auto-rejoin").checked = o.auto_rejoin !== false; $("zm-rejoin-max").value = o.rejoin_max || 5;
  setSeg($("zm-joinvia-seg"), o.join_method || "client");
  $("zm-registrant-email").placeholder = o.registrant_email_masked ? `saved: ${o.registrant_email_masked} (leave blank to keep)` : "Only for a per-registrant (tk=) link - leave blank otherwise";
  if (o.join_at) { const d = new Date(o.join_at * 1000); const pad = (n) => String(n).padStart(2, "0"); $("zm-join-at").value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; $("zm-join-at-hint").hidden = false; }
  $("zm-save").innerHTML = icon("check") + "Save changes";
  $("zm-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- library (shared source store)

function fmtWhen(ts) { if (!ts) return "never joined"; const s = (Date.now() / 1000) - ts; return "joined " + (s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : new Date(ts * 1000).toLocaleDateString()); }
function fmtJoinAt(ts) { const d = new Date(ts * 1000); return d.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }

function renderLibrary() {
  const box = $("zm-library"); const list = meetings.slice().sort((a, b) => (b.id === activeId) - (a.id === activeId) || a.name.localeCompare(b.name));
  $("zm-library-empty").hidden = list.length > 0;
  box.innerHTML = list.map((m) => {
    const o = m.options || {}; const isActive = m.id === activeId; const kind = o.meeting_kind || "meeting";
    const kindIcon = kind === "webinar" ? "ticket" : kind === "pmi" ? "house" : "video";
    const missing = (m.missing || []).length ? `<div class="zm-card-missing">${icon("alert-triangle", "icon-sm")}<span>Missing ${esc(m.missing.join("; "))}</span></div>` : "";
    const warnings = (m.warnings || []).length ? `<div class="zm-card-warning">${icon("alert-triangle", "icon-sm")}<span>${esc(m.warnings.join(" "))}</span></div>` : "";
    const joinVia = { client: "Desktop client", web: "Web client", auto: "Auto" }[o.join_method] || "Desktop client";
    const lastVia = o.last_join_method ? ` (last joined via ${o.last_join_method === "web" ? "web" : "desktop"})` : "";
    const acct = m.account_id ? (($("zm-account").querySelector(`option[value="${m.account_id}"]`) || {}).textContent || "account").replace(/\s*\(.*\)$/, "") : "guest";
    const reg = m.link_kind === "registration" ? `
      <div class="zm-card-reg">
        <button class="btn btn-secondary btn-sm" data-act="register" data-id="${m.id}">${icon("clipboard-list", "icon-sm")}Open registration</button>
        <div class="input-group mt-2"><input class="input" type="url" placeholder="Paste the personal join link Zoom issued" data-joinurl="${m.id}" autocomplete="off"><button class="btn btn-primary btn-sm" data-act="save-joinurl" data-id="${m.id}">${icon("check", "icon-sm")}Save</button></div>
      </div>` : "";
    return `
    <article class="zm-card ${isActive ? "is-active" : ""}" role="listitem" data-id="${m.id}">
      <div class="zm-card-head">
        <span class="zm-kind zm-kind-${kind}">${icon(kindIcon, "icon-sm")}${esc(KIND[kind] || kind)}</span>
        <span class="zm-card-name">${esc(m.name)}</span>
        ${isActive ? `<span class="badge badge-active zm-card-active"><span class="dot"></span>active</span>` : ""}
      </div>
      <div class="zm-card-meta">
        <span class="readout">${esc(m.meeting_id_formatted || (m.link_kind === "registration" ? "registration page" : "—"))}</span>
        <span class="zm-card-secret">${o.has_passcode ? `${icon("lock-keyhole", "icon-sm")}<span class="readout" data-secret="${m.id}">${esc(o.passcode_masked || "••••••")}</span><button class="btn btn-ghost btn-icon btn-sm" data-act="reveal" data-id="${m.id}" aria-label="Reveal passcode and link">${icon("eye", "icon-sm")}</button>` : `<span class="hint m-0">no passcode</span>`}</span>
        <span class="hint m-0">${icon("user", "icon-sm")}${esc(acct)}</span>
        <span class="hint m-0">${icon("globe", "icon-sm")}${esc(joinVia)}${esc(lastVia)}</span>
        <span class="hint m-0 zm-card-when">${esc(fmtWhen(m.last_joined_at))}</span>
        ${o.join_at ? `<span class="hint m-0 zm-card-sched">${icon("calendar-clock", "icon-sm")}${esc(fmtJoinAt(o.join_at))}${o.join_at * 1000 < Date.now() ? " (overdue)" : ""}</span>` : ""}
      </div>
      <div class="zm-card-revealed hint mono" data-revealed="${m.id}" hidden></div>
      ${missing}${warnings}${reg}
      <div class="zm-card-actions">
        <button class="btn ${isActive ? "btn-secondary" : "btn-primary"} btn-sm" data-act="switch" data-id="${m.id}" ${m.missing && m.missing.length ? "disabled" : ""}>${icon("play", "icon-sm")}${isActive ? "Active" : "Join"}</button>
        <button class="btn btn-secondary btn-sm" data-act="edit" data-id="${m.id}">${icon("pencil", "icon-sm")}Edit</button>
        <button class="btn btn-ghost btn-sm" data-act="dup" data-id="${m.id}">${icon("copy", "icon-sm")}Duplicate</button>
        <button class="btn btn-ghost btn-sm zm-danger" data-act="del" data-id="${m.id}" aria-label="Delete ${esc(m.name)}">${icon("trash-2", "icon-sm")}</button>
      </div>
    </article>`;
  }).join("");
  renderActiveHeader();
}

function renderActiveHeader() {
  const a = active();
  $("zm-active").hidden = !a; $("zm-active-empty").hidden = !!a;
  if (!a) return;
  const o = a.options || {}; const kind = o.meeting_kind || "meeting";
  $("zm-active-kind").className = "zm-kind zm-kind-" + kind; $("zm-active-kind").innerHTML = icon(kind === "webinar" ? "ticket" : kind === "pmi" ? "house" : "video", "icon-sm") + esc(KIND[kind] || kind);
  $("zm-active-name").textContent = a.name;
  $("zm-active-id").textContent = a.meeting_id_formatted || "";
  $("zm-active-meta").textContent = [o.bot_name ? `as "${o.bot_name}"` : "", o.signin_mode === "google" ? "Google account" : "guest", fmtWhen(a.last_joined_at)].filter(Boolean).join(" · ");
  const warn = $("zm-active-warning");
  if ((a.warnings || []).length) { warn.hidden = false; warn.innerHTML = icon("alert-triangle", "icon-sm") + `<span>${esc(a.warnings.join(" "))}</span>`; }
  else warn.hidden = true;
}

$("zm-library").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const id = Number(b.dataset.id);
  const run = {
    switch: () => switchTo(id), edit: () => startEdit(id), dup: () => duplicate(id), del: () => remove(id),
    reveal: () => reveal(id), register: () => openRegistration(id), "save-joinurl": () => saveJoinUrl(id),
  }[b.dataset.act];
  if (run) await withLoading(b, run);
});

async function switchTo(id) {
  const m = meetings.find((x) => x.id === id); if (!m) return;
  let preview = null; try { preview = await apiFetch(`/api/sources/${id}/switch-preview`); } catch (err) { /* generic confirm below */ }
  if (preview && preview.missing && preview.missing.length) { toast("Can't join yet - missing " + preview.missing.join("; "), "err"); return; }
  if (isLive()) {
    const msg = preview && preview.rtmp_would_drop
      ? `Switch to "${m.name}" while LIVE? This restarts the encoder, so RTMP drops for a few seconds.`
      : `Switch to "${m.name}" while LIVE? The stream stays connected; viewers see the slate while Zoom joins.`;
    if (!(await confirmDialog(msg, { confirmText: "Switch and join" }))) return;
  }
  try {
    const r = await post(`/api/sources/${id}/switch`);
    toast(r.rtmp_dropped ? `Joining "${m.name}" - encoder restarted` : `Joining "${m.name}"`);
    await refreshSources(true); setTimeout(pollStatus, 1500); setTimeout(pollJoinOptions, 6000);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) { toast(err.message, "err"); }
}
async function duplicate(id) {
  try { const r = await post(`/api/sources/${id}/duplicate`); toast(`Duplicated as "${r.source.name}"`); await refreshSources(true); startEdit(r.id); }
  catch (err) { toast(err.message, "err"); }
}
async function remove(id) {
  const m = meetings.find((x) => x.id === id); if (!m) return;
  if (!(await confirmDialog(`Delete "${m.name}"? It disappears from Sources on Remote too.`, { danger: true, confirmText: "Delete" }))) return;
  try { await apiFetch(`/api/sources/${id}`, { method: "DELETE" }); toast("Deleted"); if (editingId === id) resetEditor(); await refreshSources(true); }
  catch (err) { toast(err.message, "err"); }
}
async function reveal(id) {
  try {
    const s = await apiFetch(`/api/sources/${id}/reveal`);
    const el = document.querySelector(`[data-revealed="${id}"]`); const pc = document.querySelector(`[data-secret="${id}"]`);
    if (pc && s.passcode) pc.textContent = s.passcode;
    if (el) { el.hidden = false; el.textContent = [s.url, s.join_url ? "personal link: " + s.join_url : ""].filter(Boolean).join("\n"); }
    setTimeout(() => { renderLibrary(); }, 20000);   // re-mask after 20 s
    toast("Revealed for 20 seconds (logged in the audit trail)");
  } catch (err) { toast(err.message, "err"); }
}
async function openRegistration(id) {
  try { await post(`/api/sources/${id}/registration/open`); toast("Registration page opened on the remote screen - complete it there"); vncConnect(); $("zm-vnc").scrollIntoView({ behavior: "smooth", block: "center" }); }
  catch (err) { toast(err.message, "err"); }
}
async function saveJoinUrl(id) {
  const inp = document.querySelector(`[data-joinurl="${id}"]`); const v = inp ? inp.value.trim() : "";
  if (!v) { toast("Paste the personal join link first", "err"); return; }
  try { const r = await post(`/api/sources/${id}/join-url`, { join_url: v }); toast(r.join_ready ? "Personal join link saved - this meeting can join now" : "Saved"); await refreshSources(true); }
  catch (err) { toast(err.message, "err"); }
}

// ---------------------------------------------------------------- live sync with /remote (one store, one revision)

let syncTimer = null;
async function refreshSources(force) {
  if (document.hidden && !force) return;
  let r; try { r = await apiFetch(`/api/sources?since=${force ? -1 : rev}`); } catch (err) { return; }
  if (!r.changed) return;
  const external = !force && rev !== 0;
  rev = r.rev; meetings = (r.sources || []).filter((s) => s.type === "zoom"); activeId = r.active_id;
  renderLibrary();
  if (external) { const t = $("zm-library-sync"); t.textContent = "updated from Remote just now"; setTimeout(() => { t.textContent = "same list as Sources on Remote"; }, 4000); }
  if (lastStatus) renderStatus(lastStatus);
}

// ---------------------------------------------------------------- init

window.addEventListener("zsdash:state", (e) => { const d = e.detail; if (d && d.stream && d.stream.phase) streamPhase = d.stream.phase; });
renderLibrary(); resetEditor();
pollStatus(); statusTimer = setInterval(pollStatus, 4000);
syncTimer = setInterval(() => refreshSources(false), 3000);
pollJoinOptions();
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollStatus(); refreshSources(false); } });
