// /remote - touch-first controls. Reuses base.js's 3s /api/state poll via
// the "zsdash:state" event it dispatches rather than a second poll loop.
// Per-source panels below only poll their own thing while visible.

let SOURCES = JSON.parse(document.getElementById("sources-data").textContent);
let activeSourceId = JSON.parse(document.getElementById("active-source-id").textContent);
let sourcesRev = JSON.parse(document.getElementById("sources-rev").textContent || "0") || 0;
let streamPhase = "STOPPED";
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

function post(url, body) { return apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined }); }
function fmtTime(s) { s = Math.max(0, Math.floor(s || 0)); const m = Math.floor(s / 60), r = s % 60; return `${m}:${String(r).padStart(2, "0")}`; }
function sourceById(id) { return SOURCES.find((s) => s.id === Number(id)); }

// ---------------------------------------------------------------- stream

document.getElementById("r-go-live").addEventListener("click", (e) => streamAction(e.currentTarget, "go-live"));
document.getElementById("r-stop").addEventListener("click", (e) => streamAction(e.currentTarget, "stop", "Stop the live YouTube stream now?"));
document.getElementById("r-restart").addEventListener("click", (e) => streamAction(e.currentTarget, "restart", "Restart the encoder? This briefly interrupts the live stream."));

const rBrbBtn = document.getElementById("r-brb-slate");
const rBrbText = document.getElementById("r-brb-text");
let rBrbActive = false;

async function pollRemoteBrb() {
  try {
    const res = await apiFetch("/api/slate/brb");
    rBrbActive = !!res.active;
    if (rBrbBtn) {
      rBrbBtn.className = `btn btn-touch ${rBrbActive ? "btn-danger" : "btn-secondary"}`;
    }
    if (rBrbText) {
      rBrbText.textContent = rBrbActive ? "SLATE ACTIVE" : "BRB Slate";
    }
  } catch (e) {}
}

if (rBrbBtn) {
  rBrbBtn.addEventListener("click", async () => {
    try {
      const res = await post("/api/slate/brb", { action: "toggle" });
      rBrbActive = !!(res && res.state && res.state.active);
      if (rBrbBtn) {
        rBrbBtn.className = `btn btn-touch ${rBrbActive ? "btn-danger" : "btn-secondary"}`;
      }
      if (rBrbText) {
        rBrbText.textContent = rBrbActive ? "SLATE ACTIVE" : "BRB Slate";
      }
      toast(rBrbActive ? "Emergency BRB Holding Card Active" : "BRB Slate Cleared (Live Program)");
    } catch (err) {
      toast(err.message, "err");
    }
  });
  pollRemoteBrb();
  setInterval(pollRemoteBrb, 3000);
}

async function streamAction(btn, action, confirmMsg) {
  if (confirmMsg && !(await confirmDialog(confirmMsg, { danger: action !== "go-live" }))) return;
  await withLoading(btn, async () => {
    try { await post(`/api/stream/${action}`); toast(`Stream: ${action.replace("-", " ")} sent`); }
    catch (err) { toast(err.message, "err"); }
  });
}

const PHASE_LABEL = { STOPPED: "Stopped", STARTING: "Starting…", LIVE: "LIVE", RECONNECTING: "Reconnecting…", FAILED: "Failed" };
let lastState = null;

window.addEventListener("zsdash:state", (e) => {
  const d = e.detail; const phase = d && d.stream && d.stream.phase;
  if (!phase) return;
  streamPhase = phase; lastState = d;
  const canStop = phase === "LIVE" || phase === "RECONNECTING";
  document.getElementById("r-go-live").hidden = canStop;
  document.getElementById("r-stop").hidden = !canStop;
  renderProgram(d); renderRail(d);
});

// ---------------------------------------------------------------- program: state badge + elapsed + preview

function renderProgram(d) {
  const st = d.stream, badge = document.getElementById("panel-state");
  badge.className = "badge panel-state " + phaseBadgeClass(st.phase);
  document.getElementById("panel-state-text").textContent = PHASE_LABEL[st.phase] || st.phase;
  document.getElementById("panel-elapsed").textContent = st.phase === "LIVE" ? fmtUptime(st.uptime_seconds) : "";
  document.getElementById("panel-preview").dataset.phase = st.phase;  // drives the monitor tally (remote.css)
}

let previewTimer = null;
async function pollPreview() {
  // Paused while the interactive (noVNC) preview owns the box - interact.js
  // turns the thumbnail poll back on the moment Interact goes off.
  if (document.hidden || (window.zsInteract && window.zsInteract.active)) return;
  const img = document.getElementById("preview-img"), ph = document.getElementById("preview-placeholder");
  const box = document.getElementById("panel-preview"), age = document.getElementById("panel-preview-age");
  try {
    const res = await fetch(`/api/preview.jpg?t=${Date.now()}`, { credentials: "same-origin" });
    if (!res.ok) throw new Error("no preview");
    const blob = await res.blob(); const url = URL.createObjectURL(blob); const old = img.src;
    img.src = url; img.hidden = false; ph.hidden = true; box.classList.remove("is-stale");
    age.textContent = "live"; box.dataset.lastOk = String(Date.now());
    if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
  } catch (err) {
    const last = Number(box.dataset.lastOk || 0);
    if (last && Date.now() - last < 30000) { box.classList.add("is-stale"); age.textContent = "stale " + Math.round((Date.now() - last) / 1000) + "s"; }
    else { img.hidden = true; ph.hidden = false; age.textContent = ""; }
  }
}

// ---------------------------------------------------------------- status rail

const RESTART_ALARM = 3;
function setTile(id, text, cls) {
  const el = document.getElementById(id); el.textContent = text;
  const tile = el.closest(".rail-tile"); tile.classList.toggle("is-warn", cls === "warn"); tile.classList.toggle("is-bad", cls === "bad");
}
function renderRail(d) {
  const ff = d.ffmpeg, st = d.stream, sys = d.system || {};
  document.getElementById("rail-updated").textContent = ff ? "updated " + fmtAgo(ff.age_seconds) : (st.phase === "LIVE" ? "waiting for encoder stats" : "not streaming");
  if (ff) {
    setTile("rail-speed", ff.speed + "x", ff.speed < 0.95 ? "bad" : ff.speed < 1 ? "warn" : "");
    setTile("rail-fps", String(ff.fps), "");
    setTile("rail-bitrate", Math.round(ff.bitrate_kbps) + "k", "");
    setTile("rail-drop", String(ff.drop), ff.drop > 200 ? "warn" : "");
  } else { ["rail-speed", "rail-fps", "rail-bitrate", "rail-drop"].forEach((id) => setTile(id, "—", "")); }
  setTile("rail-cpu", sys.cpu_avg != null ? sys.cpu_avg + "%" : "—", sys.cpu_avg > 85 ? "bad" : sys.cpu_avg > 70 ? "warn" : "");
  setTile("rail-ram", sys.mem_percent != null ? Math.round(sys.mem_percent) + "%" : "—", sys.mem_percent > 90 ? "bad" : "");
  const n = st.restarts_last_5min, badge = document.getElementById("rail-restart-badge"), alarming = n > RESTART_ALARM;
  badge.className = "badge restart-rate-badge " + (alarming ? "badge-failed is-alarm" : "badge-inactive");
  document.getElementById("rail-restart-text").textContent = (n ?? "—") + " restart" + (n === 1 ? "" : "s") + " / 5 min";
  document.getElementById("rail-restart-total").textContent = st.restart_count ? st.restart_count + " total" : "";
  if (alarming && !badge.dataset.announced) { announce("Warning: " + n + " encoder restarts in five minutes"); badge.dataset.announced = "1"; }
  if (!alarming) delete badge.dataset.announced;
  const failed = document.getElementById("rail-failed");
  failed.hidden = st.phase !== "FAILED";
  if (st.phase === "FAILED") {
    document.getElementById("rail-failed-msg").textContent = "Encoder failed after " + st.restart_count + " restarts." + (st.cause ? " Likely cause: " + st.cause + "." : "");
    document.getElementById("rail-failed-err").textContent = st.last_error || "";
  }
}

// ---------------------------------------------------------------- mixer: level meter + stream volume

let levelTimer = null;
async function pollLevel() {
  if (document.hidden) return;
  let lv; try { lv = await apiFetch("/api/audio/level"); } catch (err) { return; }
  const meter = document.getElementById("stream-meter"), fill = document.getElementById("stream-meter-fill"), peak = document.getElementById("stream-meter-peak");
  const note = document.getElementById("stream-meter-note"), db = document.getElementById("stream-meter-db");
  if (!lv.live) {
    fill.style.setProperty("--level", "0%"); peak.style.setProperty("--peak", "0%");
    db.textContent = "— dBFS"; note.textContent = lv.age_seconds == null ? "starting meter…" : "no signal";
    meter.setAttribute("aria-valuenow", "-60"); return;
  }
  const pct = (v) => Math.max(0, Math.min(100, (v + 60) / 60 * 100));
  const rmsPct = pct(lv.rms_db), peakPct = pct(lv.peak_db);
  fill.style.setProperty("--level", rmsPct.toFixed(1) + "%"); fill.style.setProperty("--level-frac", String(Math.max(rmsPct / 100, 0.0001)));
  peak.style.setProperty("--peak", peakPct.toFixed(1) + "%");
  db.textContent = lv.rms_db.toFixed(0) + " dBFS rms · peak " + lv.peak_db.toFixed(0);
  note.textContent = lv.peak_db > -1 ? "clipping" : lv.rms_db < -50 ? "near silence (see note below)" : "";
  meter.setAttribute("aria-valuenow", String(Math.round(lv.rms_db)));
}

let streamVolDebounce = null;
const streamVol = document.getElementById("r-stream-volume");
streamVol.addEventListener("input", (e) => {
  document.getElementById("r-stream-volume-val").textContent = e.target.value + "%";
  clearTimeout(streamVolDebounce);
  streamVolDebounce = setTimeout(async () => {
    try { const d = await post("/api/audio/stream", { action: "volume", volume: Number(e.target.value) }); paintStreamAudio(d.muted, d.volume); }
    catch (err) { toast(err.message, "err"); }
  }, 250);
});

// ---------------------------------------------------------------- sources: tiles + switching

function paintTiles() {
  document.querySelectorAll(".source-tile").forEach((t) => {
    const on = Number(t.dataset.sourceId) === activeSourceId;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-pressed", String(on));
  });
}

// The tiles are rendered from SOURCES on the client so that changes made
// on the Zoom page (or anywhere else) show up here without a reload: the
// same `sources` table backs both, and GET /api/sources?since=<rev>
// answers {"changed": false} until something was written.
const escHtml = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function renderSourceTiles() {
  const box = document.getElementById("source-tiles");
  if (!SOURCES.length) {
    box.innerHTML = `<div class="empty-state">${icon("link")}<div class="empty-sub">No saved sources - add a Zoom meeting on the Zoom page, or other sources on Configuration.</div></div>`;
    return;
  }
  box.innerHTML = SOURCES.map((s) => {
    const kind = s.type === "zoom" ? ((s.options || {}).meeting_kind || "meeting") : s.type;
    const sub = s.type === "zoom"
      ? (kind === "pmi" ? "personal room" : kind) + (s.missing && s.missing.length ? " · needs " + escHtml(s.missing[0].split(" (")[0]) : "")
      : s.type;
    const ic = s.type === "zoom" ? (kind === "webinar" ? "ticket" : kind === "pmi" ? "house" : "video") : (s.type === "webpage" ? "globe" : "cast");
    return `<button type="button" class="source-tile${s.id === activeSourceId ? " is-active" : ""}" role="listitem" data-source-id="${s.id}" data-type="${s.type}" aria-pressed="${s.id === activeSourceId}">
      ${icon(ic)}<span class="source-tile-name">${escHtml(s.name)}</span><span class="source-tile-type">${sub}</span></button>`;
  }).join("");
}
async function syncSources() {
  if (document.hidden) return;
  let r; try { r = await apiFetch(`/api/sources?since=${sourcesRev}`); } catch (err) { return; }
  if (!r.changed) return;
  sourcesRev = r.rev; SOURCES = r.sources || [];
  const prevActive = activeSourceId; activeSourceId = r.active_id;
  renderSourceTiles();
  const hint = document.getElementById("source-switch-hint");
  hint.dataset.orig = hint.dataset.orig || hint.textContent;
  hint.textContent = "Sources updated (edited elsewhere - the Zoom page or Configuration)."; setTimeout(() => { hint.textContent = hint.dataset.orig; }, 4000);
  if (prevActive !== activeSourceId) showContextPanel();
}

function showContextPanel() {
  const s = sourceById(activeSourceId);
  ["webpage", "zoom", "direct"].forEach((t) => { document.getElementById(`ctx-${t}`).hidden = !s || s.type !== t; });
  stopPanelPolls();
  if (!s) return;
  document.getElementById(`ctx-${s.type}-name`).textContent = `- ${s.name}`;
  if (s.type === "webpage") {
    document.getElementById("ctx-account").value = s.account_id || "";
    ytPoll(); ytTimer = setInterval(ytPoll, 2000);
  } else if (s.type === "zoom") {
    document.getElementById("zoom-registration").hidden = !(s.link_kind === "registration");
    canvasPoll();  // the always-on canvas poll (below) also feeds this panel
  } else if (s.type === "direct") {
    document.getElementById("direct-url").textContent = s.url;
    const o = s.options || {};
    document.getElementById("direct-options").textContent = `mode: ${o.mode || "reencode"} · loop: ${o.loop ? "on" : "off"} · reconnect: ${o.reconnect === false ? "off" : "on"}`;
  }
}

let ytTimer = null, zoomTimer = null;
function stopPanelPolls() { clearInterval(ytTimer); clearInterval(zoomTimer); ytTimer = zoomTimer = null; }

document.getElementById("source-tiles").addEventListener("click", async (e) => {
  const tile = e.target.closest(".source-tile");
  if (!tile) return;
  const id = Number(tile.dataset.sourceId);
  const s = sourceById(id);
  let preview = {};
  try { preview = await apiFetch(`/api/sources/${id}/switch-preview`); } catch (err) { /* proceed with a generic confirm */ }
  if (preview.join_ready === false) {
    toast("This Zoom source still needs its personal join link - open it below to complete registration", "err");
    activeSourceId = id; paintTiles(); showContextPanel();
    return;
  }
  if (preview.ffmpeg_up) {
    const msg = preview.rtmp_would_drop
      ? `Switch to "${s.name}"? This kind of switch RESTARTS the encoder - viewers see a slate and a few seconds of buffering.`
      : `Switch to "${s.name}" while live? The stream stays connected; viewers see a brief slate while the picture changes.`;
    if (!(await confirmDialog(msg, { danger: preview.rtmp_would_drop, confirmText: "Switch" }))) return;
  }
  const prev = activeSourceId;
  activeSourceId = id; paintTiles();               // optimistic
  await withLoading(tile, async () => {
    try {
      const r = await post(`/api/sources/${id}/switch`);
      toast(r.hot_swapped ? "Switched - page navigated in place, stream untouched"
          : r.rtmp_dropped ? "Switched - encoder restarted" : "Switched - stream stayed connected");
      announce(`Source switched to ${s.name}`);
      showContextPanel();
    } catch (err) {
      activeSourceId = prev; paintTiles();           // roll back
      toast(err.message, "err");
    }
  });
});

// ---------------------------------------------------------------- YouTube / web page panel

document.getElementById("ctx-account").addEventListener("change", async (e) => {
  const s = sourceById(activeSourceId); if (!s) return;
  const prevVal = s.account_id || "";
  const who = e.target.selectedOptions[0] ? e.target.selectedOptions[0].textContent.split(" · ")[0] : "the shared profile";
  // Chrome can't switch profile in place: applying now relaunches only
  // the browser; the encoder keeps running and RTMP stays up.
  const live = streamPhase === "LIVE" || streamPhase === "RECONNECTING";
  let applyNow = true;
  if (live) applyNow = await confirmDialog(`Play as ${who} now? The browser relaunches on that profile - viewers see the page reload for a few seconds; the encoder keeps running. Choose "Later" to apply on the next switch instead.`, { confirmText: "Relaunch now" });
  try {
    const r = await post(`/api/sources/${s.id}/account`, { account_id: e.target.value || null, apply_now: applyNow });
    s.account_id = e.target.value ? Number(e.target.value) : null;
    toast(r.applied ? `Now playing as ${who} - browser relaunching` : `Bound to ${who} - applies on the next switch to this source`);
    if (r.applied) { browserConnected = null; setTimeout(browserStatus, 5000); }
  } catch (err) { e.target.value = prevVal; toast(err.message, "err"); }
});

let seekDragging = false;
const seek = document.getElementById("yt-seek");
seek.addEventListener("pointerdown", () => { seekDragging = true; });
seek.addEventListener("change", async () => {
  seekDragging = false;
  try { await post("/api/youtube/control", { action: "seek", seconds: Number(seek.value) }); }
  catch (err) { toast(err.message, "err"); }
});
seek.addEventListener("input", () => { document.getElementById("yt-cur").textContent = fmtTime(seek.value); });

// Browser connection line: "connected to Chrome/N - <page>" or the
// concrete reason it isn't (stale launch without the port, unit down,
// still starting, ...) plus the one-tap fix, straight from
// /api/browser/status. Re-queried whenever reachability flips.
let browserConnected = null;
async function browserStatus() {
  let b;
  try { b = await apiFetch("/api/browser/status"); } catch (err) { return; }
  const badge = document.getElementById("browser-conn-badge"), text = document.getElementById("browser-conn-text");
  const detail = document.getElementById("browser-conn-detail"), restart = document.getElementById("browser-conn-restart");
  if (b.connected) {
    badge.className = "badge badge-active";
    text.textContent = "Connected to " + (b.browser || "Chrome");
    detail.textContent = b.title ? "showing: " + b.title : "";
    restart.hidden = true;
  } else {
    badge.className = "badge " + (b.code === "starting" ? "badge-activating" : "badge-failed");
    text.textContent = b.code === "starting" ? "Browser starting…" : "Browser not reachable";
    detail.textContent = (b.reason || "") + (b.fix ? " " + b.fix : "");
    restart.hidden = !b.can_restart;
  }
}
document.getElementById("browser-conn-restart").addEventListener("click", async (e) => {
  const live = streamPhase === "LIVE" || streamPhase === "RECONNECTING";
  if (!(await confirmDialog(live
      ? "Restart the browser source while LIVE? Viewers see the page reload for a few seconds; the encoder keeps running."
      : "Restart the browser source? Chrome relaunches with the control port.", { confirmText: "Restart" }))) return;
  await withLoading(e.currentTarget, async () => {
    try { await post("/api/units/browser-source/restart"); toast("Browser source restarting"); browserConnected = null; setTimeout(browserStatus, 4000); }
    catch (err) { toast(err.message, "err"); }
  });
});

// "Open Google": google.com in the profile that already holds the
// operator's Google session - navigates the kiosk tab when it's
// controllable, otherwise opens a window in that same profile. The
// server reports which account the page shows (masked) when it can.
window.zsOpenBrowser = async function (url) {
  const r = await post("/api/browser/open", url ? { url } : {});
  const who = r.signed_in_as ? " · signed in as " + r.signed_in_as : (r.mode === "kiosk" ? " · no Google sign-in on this profile" : "");
  const where = r.mode === "kiosk" ? "Google loaded in the browser" : r.mode === "handoff" ? "Google opened in a new browser window" : "Browser opened at Google";
  toast(where + who);
  const idEl = document.getElementById("browser-identity");
  if (idEl) idEl.textContent = r.signed_in_as ? "Signed in as " + r.signed_in_as : (r.mode === "kiosk" ? "Not signed in - sign in once on the remote screen; it persists." : "");
  return r;
};
document.getElementById("browser-open-google").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, async () => {
    try { await window.zsOpenBrowser(); } catch (err) { toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- YouTube deck (web source context)
//
// Player / Add / Library tabs. Everything shown about the player is read
// off the kiosk tab over DevTools (/api/youtube/state -> phase +
// guidance); the library is the youtube_links table; automation
// (skip ads, dismiss the inactivity prompt, auto-advance) runs in the
// dashboard's watcher, these switches only set it.

const ytEsc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let ytLinks = JSON.parse(document.getElementById("yt-links-data").textContent || "[]");
let ytCurrentId = null;
let ytEditingId = null;
let ytParsed = null;
const YT_KIND = { video: ["play", "Video"], playlist: ["list-checks", "Playlist"], live_channel: ["radio", "Live channel"] };
const YT_PHASE_ICON = { idle: "cast", loading: "hourglass", playing: "play", paused: "pause", buffering: "hourglass", ad: "circle-slash", ended: "circle-check",
  prompt: "circle-help", bot_check: "alert-triangle", signin: "log-in", age: "lock-keyhole", error: "alert-triangle", no_player: "globe", offline: "wifi-off" };
const YT_ACTION_BTN = {
  "skip-ad": `<button class="btn btn-primary btn-sm" data-ytact="skip-ad">${icon("skip-forward", "icon-sm")}Skip ad</button>`,
  "dismiss-prompt": `<button class="btn btn-primary btn-sm" data-ytact="dismiss-prompt">${icon("circle-check", "icon-sm")}Continue watching</button>`,
  replay: `<button class="btn btn-secondary btn-sm" data-ytact="replay">${icon("rotate-cw", "icon-sm")}Replay</button>`,
  next: `<button class="btn btn-secondary btn-sm" data-ytact="next">${icon("skip-forward", "icon-sm")}Next in library</button>`,
  retry: `<button class="btn btn-secondary btn-sm" data-ytact="retry">${icon("rotate-cw", "icon-sm")}Retry</button>`,
  play: `<button class="btn btn-primary btn-sm" data-ytact="play">${icon("play", "icon-sm")}Play</button>`,
  pause: ``,
  "play-as": `<button class="btn btn-primary btn-sm" data-ytact="play-as">${icon("user", "icon-sm")}Play as a signed-in account</button>`,
  accounts: `<a href="/accounts" class="btn btn-secondary btn-sm">${icon("log-in", "icon-sm")}Accounts</a>`,
  "restart-browser": `<button class="btn btn-secondary btn-sm" data-ytact="restart-browser">${icon("rotate-cw", "icon-sm")}Restart browser source</button>`,
};

// tabs
const ytTabs = document.querySelector(".yt-tabs");
function ytShowTab(name) {
  ytTabs.querySelectorAll(".seg-btn").forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle("is-active", on); b.setAttribute("aria-selected", String(on)); });
  document.querySelectorAll(".yt-tab").forEach((t) => { t.hidden = t.dataset.tab !== name; });
  try { localStorage.setItem("zs-yt-tab", name); } catch (err) { /* private mode */ }
}
ytTabs.addEventListener("click", (e) => { const b = e.target.closest(".seg-btn"); if (b) ytShowTab(b.dataset.tab); });
try { ytShowTab(localStorage.getItem("zs-yt-tab") || "player"); } catch (err) { ytShowTab("player"); }
document.getElementById("ctx-account").addEventListener("focus", () => { document.getElementById("ctx-account-hint").hidden = false; });

// ---------------------------------------------------------------- player state

let ytLastPhase = null;
async function ytPoll() {
  let st;
  try { st = await apiFetch("/api/youtube/state"); } catch (err) { return; }
  if (st.available !== browserConnected) { browserConnected = st.available; browserStatus(); }
  renderYtHero(st);
  const diag = document.getElementById("yt-diagnosis");
  if (!st.available) { diag.hidden = true; return; }
  const fsBtn = document.getElementById("r-yt-fullscreen");
  fsBtn.setAttribute("aria-pressed", String(!!st.fullscreen));
  fsBtn.classList.toggle("btn-primary", !!st.fullscreen); fsBtn.classList.toggle("btn-secondary", !st.fullscreen);
  if (st.has_video) {
    if (!seekDragging) {
      seek.max = Math.max(1, Math.floor(st.duration || 0));
      seek.value = Math.floor(st.current_time || 0);
      document.getElementById("yt-cur").textContent = fmtTime(st.current_time);
    }
    document.getElementById("yt-dur").textContent = st.duration ? fmtTime(st.duration) : "live";
    seek.disabled = !st.duration;
    const muteBtn = document.getElementById("r-yt-mute");
    muteBtn.setAttribute("aria-pressed", String(!!st.muted));
    muteBtn.innerHTML = icon(st.muted ? "volume-x" : "volume-2");
    muteBtn.classList.toggle("btn-danger", !!st.muted); muteBtn.classList.toggle("btn-secondary", !st.muted);
    const vol = document.getElementById("r-yt-volume");
    if (document.activeElement !== vol) vol.value = st.volume ?? 100;
    const cc = document.getElementById("r-yt-cc");
    cc.setAttribute("aria-pressed", String(!!st.captions)); cc.classList.toggle("btn-primary", !!st.captions); cc.classList.toggle("btn-secondary", !st.captions);
    const sp = String(st.speed || 1).replace(/\.0$/, "");
    document.querySelectorAll("#yt-speed-seg .seg-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.value === sp));
  }
  if (st.current_link_id !== undefined && st.current_link_id !== ytCurrentId) { ytCurrentId = st.current_link_id; renderYtLibrary(); }
  // diagnosis banner is subsumed by the hero guidance; keep it for the Accounts link on sign-in walls
  diag.hidden = true;
}

function renderYtHero(st) {
  const hero = document.getElementById("yt-hero");
  const phase = st.phase || (st.available ? "idle" : "offline");
  const changed = hero.dataset.phase !== phase;
  hero.dataset.phase = phase; hero.dataset.tone = st.tone || "idle";
  document.getElementById("yt-hero-icon").innerHTML = icon(YT_PHASE_ICON[phase] || "cast");
  document.getElementById("yt-phase-label").textContent = st.label || phase;
  document.getElementById("yt-live-badge").hidden = !st.live;
  const cur = ytLinks.find((l) => l.id === ytCurrentId);
  const title = st.has_video ? (cur && cur.title ? cur.title : (st.title || "").replace(/ - YouTube$/, "")) : (st.available ? "" : "");
  document.getElementById("yt-title").textContent = title || (phase === "no_player" ? (st.title || "") : "—");
  document.getElementById("yt-author").textContent = cur && cur.author ? cur.author : "";
  document.getElementById("yt-quality").textContent = st.quality ? st.quality : "";
  document.getElementById("yt-speed-badge").textContent = st.speed && st.speed !== 1 ? st.speed + "×" : "";
  document.getElementById("yt-cc-badge").hidden = !st.captions;
  const thumb = document.getElementById("yt-hero-thumb");
  const art = cur && cur.thumbnail_url ? cur.thumbnail_url : "";
  if (art) { if (thumb.src !== art) thumb.src = art; thumb.hidden = false; } else { thumb.hidden = true; thumb.removeAttribute("src"); }
  const g = document.getElementById("yt-guidance");
  if (st.guide) {
    g.hidden = false; g.className = "banner yt-guidance tone-" + (st.tone || "idle");
    document.getElementById("yt-guidance-text").textContent = st.guide;
    document.getElementById("yt-guidance-actions").innerHTML = (st.actions || []).map((a) => YT_ACTION_BTN[a] || "").join("");
  } else g.hidden = true;
  const log = document.getElementById("yt-auto-log");
  if (st.auto && st.auto.length) { log.hidden = false; log.textContent = "Automatic: " + st.auto.join(" · "); } else log.hidden = true;
  if (changed && ytLastPhase !== null && ["ended", "bot_check", "signin", "age", "error", "prompt", "offline"].includes(phase)) announce("YouTube: " + (st.label || phase));
  ytLastPhase = phase;
}

const ytControl = (action, extra) => async (e) => {
  try { const r = await post("/api/youtube/control", Object.assign({ action }, extra || {})); if (r && r.current_link_id !== undefined) { ytCurrentId = r.current_link_id; renderYtLibrary(); } ytPoll(); }
  catch (err) { toast(err.message, "err"); }
};
document.getElementById("r-yt-play").addEventListener("click", ytControl("play"));
document.getElementById("r-yt-pause").addEventListener("click", ytControl("pause"));
document.getElementById("r-yt-theater").addEventListener("click", ytControl("theater"));
document.getElementById("r-yt-fullscreen").addEventListener("click", ytControl("fullscreen"));
document.getElementById("r-yt-cc").addEventListener("click", ytControl("captions"));
document.getElementById("r-yt-prev").addEventListener("click", (e) => withLoading(e.currentTarget, ytControl("prev")));
document.getElementById("r-yt-next").addEventListener("click", (e) => withLoading(e.currentTarget, ytControl("next")));
document.getElementById("r-yt-mute").addEventListener("click", (e) => ytControl(e.currentTarget.getAttribute("aria-pressed") === "true" ? "unmute" : "mute")(e));
initSegmented(document.getElementById("yt-speed-seg"), (v) => ytControl("speed", { rate: Number(v) })());
let volumeDebounce = null;
document.getElementById("r-yt-volume").addEventListener("input", (e) => {
  clearTimeout(volumeDebounce);
  const level = Number(e.target.value);
  volumeDebounce = setTimeout(() => post("/api/youtube/control", { action: "volume", level }).catch((err) => toast(err.message, "err")), 200);
});
document.getElementById("yt-guidance-actions").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-ytact]"); if (!b) return;
  const act = b.dataset.ytact;
  if (act === "play-as") { ytShowTab("player"); document.getElementById("ctx-account").focus(); document.getElementById("ctx-account-hint").hidden = false; return; }
  if (act === "restart-browser") { document.getElementById("browser-conn-restart").click(); return; }
  await withLoading(b, ytControl(act));
});

// automation switches
document.getElementById("yt-automation").addEventListener("change", async (e) => {
  const inp = e.target.closest("input[data-setting]"); if (!inp) return;
  try { await apiFetch("/api/youtube/settings", { method: "PUT", body: JSON.stringify({ [inp.dataset.setting]: inp.checked }) }); toast(inp.checked ? "On" : "Off"); }
  catch (err) { inp.checked = !inp.checked; toast(err.message, "err"); }
});

// ---------------------------------------------------------------- Add: smart paste

let ytParseTimer = null;
const ytPaste = document.getElementById("yt-paste");
ytPaste.addEventListener("input", () => { clearTimeout(ytParseTimer); ytParseTimer = setTimeout(ytDetect, 500); });
ytPaste.addEventListener("paste", () => { clearTimeout(ytParseTimer); ytParseTimer = setTimeout(ytDetect, 80); });
document.getElementById("yt-detect").addEventListener("click", (e) => withLoading(e.currentTarget, ytDetect));

async function ytDetect() {
  const text = ytPaste.value.trim();
  const status = document.getElementById("yt-detect-status");
  if (!text) { ytParsed = null; document.getElementById("yt-parsed").hidden = true; document.getElementById("yt-form").hidden = !ytEditingId; return; }
  status.textContent = "looking it up…";
  try { ytParsed = await post("/api/youtube/parse", { text }); } catch (err) { status.textContent = ""; toast(err.message, "err"); return; }
  status.textContent = "";
  renderYtParsed();
}
function renderYtParsed() {
  const p = ytParsed; const box = document.getElementById("yt-parsed"); box.hidden = false; box.classList.toggle("is-error", !p.ok);
  const kind = YT_KIND[p.kind] || ["circle-help", p.kind === "channel" ? "Channel page" : "Unrecognized"];
  document.getElementById("yt-parsed-kind").innerHTML = icon(kind[0], "icon-sm") + ytEsc(kind[1]) + (p.live_now === true ? " · live now" : p.live_now === false ? " · offline" : "");
  const th = document.getElementById("yt-parsed-thumb");
  if (p.thumbnail_url) { th.src = p.thumbnail_url; th.hidden = false; } else { th.hidden = true; th.removeAttribute("src"); }
  document.getElementById("yt-parsed-title").textContent = p.title || (p.ok ? (p.kind === "playlist" ? "Playlist " + p.list_id : p.kind === "live_channel" ? (p.channel || "") + " live" : "Video " + (p.video_id || "")) : "Not playable");
  document.getElementById("yt-parsed-meta").textContent = [p.author, p.video_id ? "id " + p.video_id : "", p.list_id ? "list " + p.list_id.slice(0, 12) + "…" : "", p.start ? "starts at " + p.start_formatted : ""].filter(Boolean).join(" · ");
  const notes = [...(p.errors || []).map((t) => ["bad", "alert-triangle", t]), ...(p.warnings || []).map((t) => ["warn", "info", t])];
  document.getElementById("yt-parsed-notes").innerHTML = notes.map(([tone, ic, t]) => `<li class="tone-${tone}">${icon(ic, "icon-sm")}<span>${ytEsc(t)}</span></li>`).join("");
  document.getElementById("yt-form").hidden = !p.ok;
  if (p.ok) {
    document.getElementById("yt-name").placeholder = p.title || "Name";
    if (p.start && !document.getElementById("yt-start").value) document.getElementById("yt-start").value = p.start_formatted;
  }
}
initSegmented(document.getElementById("yt-form-speed"));
function ytSegValue(seg) { const b = seg.querySelector(".seg-btn.is-active"); return b ? Number(b.dataset.value) : 1; }
function ytSetSeg(seg, v) { seg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", Number(b.dataset.value) === Number(v))); }
function ytReadForm() {
  return { name: document.getElementById("yt-name").value.trim(), url: ytParsed ? ytParsed.url : "",
    options: { start: document.getElementById("yt-start").value.trim() || null, loop: document.getElementById("yt-loop").checked, captions: document.getElementById("yt-captions").checked, speed: ytSegValue(document.getElementById("yt-form-speed")) } };
}
function ytResetForm() {
  ytEditingId = null; ytParsed = null;
  ytPaste.value = ""; document.getElementById("yt-parsed").hidden = true; document.getElementById("yt-form").hidden = true;
  document.getElementById("yt-name").value = ""; document.getElementById("yt-start").value = ""; document.getElementById("yt-loop").checked = false; document.getElementById("yt-captions").checked = false;
  ytSetSeg(document.getElementById("yt-form-speed"), 1); document.getElementById("yt-form-error").hidden = true;
  document.getElementById("yt-edit-cancel").hidden = true; document.getElementById("yt-save").innerHTML = icon("check") + "Save"; document.getElementById("yt-play-once").hidden = false;
}
async function ytSave(thenPlay) {
  const err = document.getElementById("yt-form-error"); err.hidden = true;
  if (!ytEditingId && !(ytParsed && ytParsed.ok)) { err.hidden = false; err.textContent = "Paste a playable YouTube link first."; return; }
  const body = ytReadForm();
  try {
    let link;
    if (ytEditingId) link = (await apiFetch(`/api/youtube-links/${ytEditingId}`, { method: "PUT", body: JSON.stringify(body) })).link;
    else link = (await post("/api/youtube-links", body)).link;
    await ytReloadLinks();
    toast(ytEditingId ? "Saved changes" : `Saved "${link.name}"`);
    ytResetForm();
    if (thenPlay) await ytPlayLink(link.id); else ytShowTab("library");
  } catch (e2) { err.hidden = false; err.textContent = e2.message; }
}
document.getElementById("yt-save").addEventListener("click", (e) => withLoading(e.currentTarget, () => ytSave(false)));
document.getElementById("yt-save-play").addEventListener("click", (e) => withLoading(e.currentTarget, () => ytSave(true)));
document.getElementById("yt-play-once").addEventListener("click", (e) => withLoading(e.currentTarget, async () => {
  if (!(ytParsed && ytParsed.ok)) { toast("Paste a playable YouTube link first", "err"); return; }
  const body = ytReadForm();
  try { await post("/api/youtube/play", { url: body.url, options: body.options }); ytCurrentId = null; renderYtLibrary(); toast("Playing"); ytShowTab("player"); setTimeout(ytPoll, 1200); }
  catch (err) { toast(err.message, "err"); }
}));
document.getElementById("yt-edit-cancel").addEventListener("click", ytResetForm);

function ytStartEdit(id) {
  const l = ytLinks.find((x) => x.id === id); if (!l) return;
  ytResetForm(); ytEditingId = id;
  ytParsed = { ok: true, kind: l.kind, url: l.url, title: l.title, author: l.author, thumbnail_url: l.thumbnail_url, video_id: l.video_id, list_id: null, start: (l.options || {}).start, start_formatted: fmtTime((l.options || {}).start || 0), warnings: [], errors: [] };
  ytPaste.value = l.url; renderYtParsed();
  document.getElementById("yt-name").value = l.name; document.getElementById("yt-start").value = (l.options || {}).start ? fmtTime(l.options.start) : "";
  document.getElementById("yt-loop").checked = !!(l.options || {}).loop; document.getElementById("yt-captions").checked = !!(l.options || {}).captions; ytSetSeg(document.getElementById("yt-form-speed"), (l.options || {}).speed || 1);
  document.getElementById("yt-edit-cancel").hidden = false; document.getElementById("yt-save").innerHTML = icon("check") + "Save changes"; document.getElementById("yt-play-once").hidden = true;
  ytShowTab("add"); document.getElementById("ctx-webpage").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- Library

function fmtPlayed(ts) { if (!ts) return "never played"; const s = Date.now() / 1000 - ts; return "played " + (s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : new Date(ts * 1000).toLocaleDateString()); }
function renderYtLibrary() {
  const box = document.getElementById("r-yt-list");
  document.getElementById("r-yt-empty").hidden = ytLinks.length > 0;
  document.getElementById("yt-lib-count").textContent = ytLinks.length;
  const curIdx = ytLinks.findIndex((l) => l.id === ytCurrentId);
  box.innerHTML = ytLinks.map((l, i) => {
    const kind = YT_KIND[l.kind] || YT_KIND.video; const o = l.options || {};
    const opts = [o.start ? "from " + fmtTime(o.start) : "", o.loop ? "loop" : "", o.captions ? "CC" : "", o.speed && o.speed !== 1 ? o.speed + "×" : ""].filter(Boolean).join(" · ");
    const isCur = l.id === ytCurrentId, isNext = curIdx >= 0 && i === (curIdx + 1) % ytLinks.length && ytLinks.length > 1;
    return `<article class="yt-card ${isCur ? "is-current" : ""}" role="listitem" data-link-id="${l.id}">
      <button type="button" class="yt-card-play" aria-label="Play ${ytEsc(l.name)}">
        ${l.thumbnail_url ? `<img src="${ytEsc(l.thumbnail_url)}" alt="" loading="lazy" class="yt-thumb">` : `<div class="yt-thumb yt-thumb-empty">${icon(kind[0])}</div>`}
        <span class="yt-kind yt-kind-${l.kind}">${icon(kind[0], "icon-sm")}${kind[1]}</span>
        ${isCur ? `<span class="yt-card-flag is-now">${icon("play", "icon-sm")}Now</span>` : isNext ? `<span class="yt-card-flag">Up next</span>` : ""}
        <span class="yt-card-name">${ytEsc(l.name)}</span>
      </button>
      <div class="yt-card-body">
        <div class="hint yt-card-meta">${ytEsc(l.author || "")}${l.author && opts ? " · " : ""}${ytEsc(opts)}</div>
        <div class="hint yt-card-when">${fmtPlayed(l.last_played_at)}${l.plays ? " · " + l.plays + "×" : ""}</div>
        <div class="yt-card-actions">
          <button class="btn btn-ghost btn-icon btn-sm" data-ytlib="up" data-id="${l.id}" aria-label="Move up" ${i === 0 ? "disabled" : ""}>${icon("chevron-up", "icon-sm")}</button>
          <button class="btn btn-ghost btn-icon btn-sm" data-ytlib="down" data-id="${l.id}" aria-label="Move down" ${i === ytLinks.length - 1 ? "disabled" : ""}>${icon("chevron-down", "icon-sm")}</button>
          <button class="btn btn-ghost btn-icon btn-sm" data-ytlib="edit" data-id="${l.id}" aria-label="Edit ${ytEsc(l.name)}">${icon("pencil", "icon-sm")}</button>
          <button class="btn btn-ghost btn-icon btn-sm" data-ytlib="dup" data-id="${l.id}" aria-label="Duplicate ${ytEsc(l.name)}">${icon("copy", "icon-sm")}</button>
          <button class="btn btn-ghost btn-icon btn-sm yt-danger" data-ytlib="del" data-id="${l.id}" aria-label="Delete ${ytEsc(l.name)}">${icon("trash-2", "icon-sm")}</button>
        </div>
      </div>
    </article>`;
  }).join("");
}
async function ytReloadLinks() { try { ytLinks = await apiFetch("/api/youtube-links"); } catch (err) { /* keep what we have */ } renderYtLibrary(); }
async function ytPlayLink(id) {
  const l = ytLinks.find((x) => x.id === id); if (!l) return;
  try { const r = await post("/api/youtube/play", { link_id: id }); ytCurrentId = r.current_link_id ?? id; renderYtLibrary(); toast(`Now playing: ${l.name}`); announce("YouTube: " + l.name); ytShowTab("player"); setTimeout(ytPoll, 1200); await ytReloadLinks(); }
  catch (err) { toast(err.message, "err"); }
}
document.getElementById("r-yt-list").addEventListener("click", async (e) => {
  const act = e.target.closest("[data-ytlib]");
  if (act) {
    const id = Number(act.dataset.id); const idx = ytLinks.findIndex((l) => l.id === id);
    if (act.dataset.ytlib === "edit") { ytStartEdit(id); return; }
    if (act.dataset.ytlib === "del") {
      if (!(await confirmDialog(`Delete "${ytLinks[idx].name}" from the library?`, { danger: true, confirmText: "Delete" }))) return;
      try { await apiFetch(`/api/youtube-links/${id}`, { method: "DELETE" }); if (ytEditingId === id) ytResetForm(); await ytReloadLinks(); toast("Deleted"); } catch (err) { toast(err.message, "err"); }
      return;
    }
    if (act.dataset.ytlib === "dup") { try { await post(`/api/youtube-links/${id}/duplicate`); await ytReloadLinks(); toast("Duplicated"); } catch (err) { toast(err.message, "err"); } return; }
    if (act.dataset.ytlib === "up" || act.dataset.ytlib === "down") {
      const j = act.dataset.ytlib === "up" ? idx - 1 : idx + 1; if (j < 0 || j >= ytLinks.length) return;
      const ids = ytLinks.map((l) => l.id); [ids[idx], ids[j]] = [ids[j], ids[idx]];
      try { await post("/api/youtube-links/reorder", { ids }); await ytReloadLinks(); } catch (err) { toast(err.message, "err"); }
      return;
    }
  }
  const play = e.target.closest(".yt-card-play"); if (!play) return;
  await withLoading(play, () => ytPlayLink(Number(play.closest(".yt-card").dataset.linkId)));
});
renderYtLibrary();

// ---------------------------------------------------------------- Zoom panel

const ZOOM_STATUS_LABEL = {
  not_joined: "Not joined", connecting: "Connecting…", waiting_room: "In waiting room", not_started: "Host hasn't started",
  in_meeting: "In meeting", ended: "Meeting ended", expired: "Link expired", passcode_required: "Passcode required",
  registration_required: "Registration required", removed: "Removed by host", join_failed: "Could not join", unknown: "Unknown",
};
const ZOOM_STATUS_CLASS = { in_meeting: "badge-active", connecting: "badge-activating", waiting_room: "badge-activating", not_started: "badge-activating",
  ended: "badge-inactive", not_joined: "badge-inactive", unknown: "badge-inactive", expired: "badge-failed", passcode_required: "badge-failed", registration_required: "badge-failed", removed: "badge-failed", join_failed: "badge-failed" };

// Mic/camera buttons show what Zoom *reports* (its own toolbar button,
// read over AT-SPI): muted / unmuted / no audio / unknown. Never the
// state we'd expect after a keystroke.
const MIC_STATE_LABEL = { muted: "Mic muted", unmuted: "Mic live", no_audio: "Mic: no audio joined", unknown: "Mic: unknown" };
const CAM_STATE_LABEL = { off: "Camera off", on: "Camera on", unknown: "Camera: unknown" };
function paintZoomReadback(which, verify) {
  const btn = document.getElementById(which === "mic" ? "z-mic" : "z-camera");
  const state = (verify && verify.available && verify.state) || "unknown";
  const labels = which === "mic" ? MIC_STATE_LABEL : CAM_STATE_LABEL;
  const off = which === "mic" ? (state === "muted" || state === "no_audio") : state === "off";
  const known = state !== "unknown";
  btn.innerHTML = icon(which === "mic" ? (off ? "mic-off" : "mic") : (off ? "eye-off" : "video")) + `<span>${labels[state] || labels.unknown}</span>`;
  if (known) btn.setAttribute("aria-pressed", String(off)); else btn.removeAttribute("aria-pressed");
  btn.classList.toggle("btn-danger", known && off); btn.classList.toggle("btn-secondary", !(known && off));
}

// ---- canvas alert: polled for every source type (this is about what
// is physically on :99, e.g. a dead Zoom dialog on top of the browser).
let canvasTimer = null, lastZoomStatus = null;
const CANVAS_TITLE = {
  expired: "Zoom: link expired.", join_failed: "Zoom could not join.", removed: "Zoom: removed by host.", ended: "Zoom: meeting ended.",
  passcode_required: "Zoom needs a passcode.", registration_required: "Zoom needs registration.", waiting_room: "Zoom: in the waiting room.",
  not_started: "Zoom: host hasn't started.",
};
function renderCanvasAlert(st) {
  const box = document.getElementById("canvas-alert");
  const covering = st.covers_canvas && !(st.status === "not_joined" && st.service === "STOPPED");
  const show = st.terminal || covering;
  box.hidden = !show;
  if (!show) return;
  const title = st.terminal ? CANVAS_TITLE[st.status] || "Zoom needs attention." : "Zoom is on top of the " + (st.source_type || "active") + " source.";
  document.getElementById("canvas-alert-title").textContent = title;
  document.getElementById("canvas-alert-detail").textContent = st.terminal ? (st.detail || "") : "Its window is covering what the active source draws - viewers see Zoom, not the page.";
  document.getElementById("canvas-alert-action").textContent = st.terminal ? (st.action || "") : "Quit Zoom to clear the canvas, or switch the active source to Zoom.";
  document.getElementById("canvas-dismiss").hidden = !(st.dialogs && st.dialogs.length);
  document.getElementById("canvas-rejoin").hidden = !(st.terminal && st.source_type === "zoom");
}
async function canvasPoll() {
  if (document.hidden) return;
  let st;
  try { st = await apiFetch("/api/zoom/status"); } catch (err) { return; }
  lastZoomStatus = st;
  renderCanvasAlert(st);
  if (!document.getElementById("ctx-zoom").hidden) renderZoomPanel(st);
  if (st.mic) paintZoomMic(st.mic);
}
document.getElementById("canvas-dismiss").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post("/api/zoom/dialog", { action: "dismiss" });
      const n = (r.dismissed || []).length;
      toast(n ? `Dismissed: ${r.dismissed.map((d) => d.title).join(", ")}` : "No dialog could be dismissed", n ? "ok" : "err");
      setTimeout(canvasPoll, 800);
    } catch (err) { toast(err.message, "err"); }
  });
});
document.getElementById("canvas-quit-zoom").addEventListener("click", async (e) => {
  if (!(await confirmDialog("Quit Zoom and show the plain slate on the canvas? The encoder keeps running.", { confirmText: "Quit Zoom" }))) return;
  await withLoading(e.currentTarget, async () => {
    try { await post("/api/zoom/quit-to-slate"); toast("Zoom stopped - canvas on slate"); setTimeout(canvasPoll, 1500); }
    catch (err) { toast(err.message, "err"); }
  });
});
document.getElementById("canvas-rejoin").addEventListener("click", async (e) => {
  if (!(await confirmDialog("Rejoin the meeting? Zoom restarts and joins again."))) return;
  await withLoading(e.currentTarget, async () => {
    try { await post("/api/zoom/rejoin"); toast("Rejoin sent"); setTimeout(canvasPoll, 4000); } catch (err) { toast(err.message, "err"); }
  });
});

async function zoomPoll() { return canvasPoll(); }

function renderZoomPanel(st) {
  const badge = document.getElementById("zoom-status-badge");
  badge.className = "badge " + (ZOOM_STATUS_CLASS[st.status] || "badge-inactive");
  document.getElementById("zoom-status-text").textContent = ZOOM_STATUS_LABEL[st.status] || st.status;
  document.getElementById("zoom-status-detail").textContent = st.detail || "";
  const act = document.getElementById("zoom-status-action");
  act.hidden = !st.action; document.getElementById("zoom-status-action-text").textContent = st.action || "";
  paintZoomReadback("mic", st.mic); paintZoomReadback("camera", st.camera);
  const unverified = [];
  if (!(st.mic && st.mic.available)) unverified.push("mic");
  if (!(st.camera && st.camera.available)) unverified.push("camera");
  document.getElementById("zoom-readback").textContent = unverified.length
    ? `State unknown for ${unverified.join(" and ")} (${(st.mic && st.mic.reason) || (st.camera && st.camera.reason) || ""}) - buttons still send Zoom's shortcut.`
    : `Read back from Zoom's own toolbar buttons: "${st.mic.name || ""}" / "${st.camera.name || ""}".`;
}

const zoomShortcut = (action) => async (e) => {
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post("/api/zoom/control", { action });
      if (action === "mic" || action === "camera") paintZoomReadback(action, r.verify);
      toast(`Sent ${action.replace("-", " ")} to Zoom`); announce(`Zoom ${action} toggled`);
      setTimeout(zoomPoll, 800);
    } catch (err) { toast(err.message, "err"); }
  });
};
document.getElementById("z-mic").addEventListener("click", zoomShortcut("mic"));
document.getElementById("z-camera").addEventListener("click", zoomShortcut("camera"));
document.getElementById("z-view-speaker").addEventListener("click", zoomShortcut("view-speaker"));
document.getElementById("z-view-gallery").addEventListener("click", zoomShortcut("view-gallery"));

document.getElementById("z-join").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, async () => {
    try { await post("/api/zoom/join"); toast("Join sent"); setTimeout(zoomPoll, 3000); } catch (err) { toast(err.message, "err"); }
  });
});
document.getElementById("z-rejoin").addEventListener("click", async (e) => {
  if (!(await confirmDialog("Rejoin the meeting? Zoom restarts and joins again."))) return;
  await withLoading(e.currentTarget, async () => {
    try { await post("/api/zoom/rejoin"); toast("Rejoin sent"); setTimeout(zoomPoll, 4000); } catch (err) { toast(err.message, "err"); }
  });
});
async function zoomLeave(then, msg) {
  if (!(await confirmDialog(msg, { danger: true }))) return;
  try { await post("/api/zoom/leave-with-choice", { then }); toast(then === "stop" ? "Left the meeting and stopped the stream" : "Left the meeting - encoder now on a slate"); setTimeout(zoomPoll, 1500); }
  catch (err) { toast(err.message, "err"); }
}
document.getElementById("z-leave-slate").addEventListener("click", () => zoomLeave("slate", "Leave the meeting? The stream keeps running on a plain slate."));
document.getElementById("z-leave-stop").addEventListener("click", () => zoomLeave("stop", "Leave the meeting AND stop the stream now?"));

document.getElementById("zoom-reg-open").addEventListener("click", async (e) => {
  const s = sourceById(activeSourceId); if (!s) return;
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post(`/api/sources/${s.id}/registration/open`);
      toast(`Registration page opened on the remote desktop (profile: ${r.profile_id})`);
      window.open("/vnc", "_blank", "noopener");
    } catch (err) { toast(err.message, "err"); }
  });
});
document.getElementById("zoom-join-url-save").addEventListener("click", async (e) => {
  const s = sourceById(activeSourceId); if (!s) return;
  const url = document.getElementById("zoom-join-url").value.trim();
  if (!url) { toast("Paste the personal join link first", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post(`/api/sources/${s.id}/join-url`, { join_url: url });
      s.join_ready = r.join_ready; s.options = Object.assign({}, s.options, { join_url: url });
      toast("Join link saved - this source can now be joined");
      const tile = document.querySelector(`.source-tile[data-source-id="${s.id}"] .source-tile-type`);
      if (tile) tile.textContent = "zoom";
      document.getElementById("zoom-registration").hidden = true;
    } catch (err) { toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- Direct panel

document.getElementById("direct-probe").addEventListener("click", async (e) => {
  const s = sourceById(activeSourceId); if (!s) return;
  const out = document.getElementById("direct-probe-result");
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post("/api/sources/probe", { url: s.url });
      const v = r.video ? `${r.video.codec} ${r.video.width}x${r.video.height}${r.video.fps ? " @" + r.video.fps + "fps" : ""}` : "no video";
      out.textContent = `${v} · ${r.audio ? r.audio.codec : "no audio"}${r.bitrate_kbps ? " · " + r.bitrate_kbps + "kbps" : ""}${r.copy_safe ? " · copy-safe" : " · needs re-encode"}`;
    } catch (err) { toast(err.message, "err"); out.textContent = ""; }
  });
});

// ---------------------------------------------------------------- audio (stream mute / Zoom mic - unchanged behaviour)

const streamAudioBtn = document.getElementById("r-stream-audio");
function paintStreamAudio(muted, volume) {
  document.getElementById("stream-meter").classList.toggle("is-muted", !!muted);
  if (volume != null && document.activeElement !== streamVol) { streamVol.value = volume; document.getElementById("r-stream-volume-val").textContent = volume + "%"; }
  streamAudioBtn.innerHTML = icon(muted ? "volume-x" : "volume-2");
  streamAudioBtn.setAttribute("aria-pressed", String(muted));
  streamAudioBtn.setAttribute("aria-label", muted ? "Unmute stream audio" : "Mute stream audio");
  streamAudioBtn.classList.toggle("btn-danger", muted); streamAudioBtn.classList.toggle("btn-secondary", !muted);
}
streamAudioBtn.addEventListener("click", async () => {
  const wasMuted = streamAudioBtn.getAttribute("aria-pressed") === "true";
  paintStreamAudio(!wasMuted); streamAudioBtn.disabled = true;
  try {
    const data = await post("/api/audio/stream", { action: wasMuted ? "unmute" : "mute" });
    paintStreamAudio(data.muted, data.volume); toast(data.muted ? "Stream audio muted" : "Stream audio unmuted"); announce(data.muted ? "Stream audio muted" : "Stream audio unmuted");
  } catch (err) { paintStreamAudio(wasMuted); toast(err.message, "err"); }
  finally { streamAudioBtn.disabled = false; }
});

const zoomMicBtn = document.getElementById("r-zoom-mic"), zoomMicHint = document.getElementById("r-zoom-mic-hint");
// Muted / Unmuted / No audio / Unknown - exactly what Zoom's own toolbar
// button says (read over AT-SPI after the shortcut), never an assumption
// that the keystroke landed.
function paintZoomMic(verify) {
  const state = (verify && verify.available && verify.state) || "unknown";
  const muted = state === "muted" || state === "no_audio";
  zoomMicBtn.innerHTML = icon(muted ? "mic-off" : "mic");
  zoomMicBtn.classList.toggle("btn-danger", state === "unmuted"); zoomMicBtn.classList.toggle("btn-secondary", state !== "unmuted");
  if (state === "unknown") zoomMicBtn.removeAttribute("aria-pressed"); else zoomMicBtn.setAttribute("aria-pressed", String(muted));
  zoomMicHint.textContent = {
    muted: "Muted - read back from Zoom (\"" + (verify.name || "Unmute") + "\" button showing)",
    unmuted: "UNMUTED - read back from Zoom (\"" + (verify.name || "Mute") + "\" button showing)",
    no_audio: "No audio joined - Zoom shows \"Join Audio\", so the mic can't be live",
    unknown: "Unknown - " + ((verify && verify.reason) || "Zoom's toolbar isn't readable right now"),
  }[state];
}
zoomMicBtn.addEventListener("click", async () => {
  await withLoading(zoomMicBtn, async () => {
    try {
      const d = await post("/api/audio/zoom-mic/toggle"); paintZoomMic(d.verify);
      const s = d.verify && d.verify.available ? d.verify.state : "unknown";
      toast(s === "unknown" ? "Sent Alt+A to Zoom - state couldn't be read back" : "Zoom mic now: " + s.replace("_", " "), s === "unknown" ? "err" : "ok");
      announce("Zoom mic " + s.replace("_", " "));
    } catch (err) { toast(err.message, "err"); }
  });
});

// Sink note: whether anything is actually playing into the stream sink -
// the difference between "silent because nothing is playing" and
// "silent although a source is connected" (or "muted").
let sinkTimer = null;
async function pollSink() {
  if (document.hidden) return;
  let d; try { d = await apiFetch("/api/audio/stream"); } catch (err) { return; }
  paintStreamAudio(d.muted, d.volume);
  const note = document.getElementById("stream-sink-note");
  if (d.muted) note.textContent = "Stream audio is MUTED - viewers hear nothing regardless of the source.";
  else if (!d.input_streams) note.textContent = "Nothing is playing into the stream sink right now (zoom_out has no active input) - a silent meter here is expected, not a fault.";
  else note.textContent = `${d.input_streams} app${d.input_streams === 1 ? "" : "s"} feeding the stream sink (${(d.sink_state || "").toLowerCase()}).`;
}

document.getElementById("r-audio-selftest").addEventListener("click", async (e) => {
  const live = streamPhase === "LIVE" || streamPhase === "RECONNECTING";
  if (live) { toast("Not while live - the test tone would go out to viewers. Stop the stream first.", "err"); return; }
  if (!(await confirmDialog("Run the audio self-test? A 6-second 440 Hz tone is played into the stream sink and recorded through the encoder graph (~12 s).", { confirmText: "Run test" }))) return;
  const out = document.getElementById("r-audio-selftest-result");
  out.textContent = "running…";
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post("/api/audio/selftest");
      if (r.error) { out.textContent = r.error; toast(r.error, "err"); return; }
      out.textContent = `${r.ok ? "PASS" : "FAIL"} - meter peak ${r.meter_peak_db} dBFS · recorded file mean ${r.file_mean_volume_db} dB / max ${r.file_max_volume_db} dB (${r.file_audio_codec})`;
      toast(r.ok ? "Audio path OK: tone reached the meter and the recording" : "Audio self-test failed - see result", r.ok ? "ok" : "err");
    } catch (err) { out.textContent = err.message; toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- init

pollSink(); sinkTimer = setInterval(pollSink, 5000);
pollPreview(); previewTimer = setInterval(pollPreview, 3000);
pollLevel(); levelTimer = setInterval(pollLevel, 1000);
// Zoom status is polled whatever the active source is: it drives the
// canvas alert (a Zoom dialog on top of the browser is a program-panel
// problem, not a Zoom-panel one) and the mixer's mic readback.
canvasPoll(); canvasTimer = setInterval(canvasPoll, 5000);
renderSourceTiles();
showContextPanel();
setInterval(syncSources, 3000);
document.addEventListener("visibilitychange", () => { if (document.hidden) stopPanelPolls(); else { showContextPanel(); canvasPoll(); pollSink(); syncSources(); } });
