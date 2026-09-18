// /remote - touch-first controls. Reuses base.js's 3s /api/state poll via
// the "zsdash:state" event it dispatches rather than a second poll loop.
// Per-source panels below only poll their own thing while visible.

const SOURCES = JSON.parse(document.getElementById("sources-data").textContent);
let activeSourceId = JSON.parse(document.getElementById("active-source-id").textContent);
let streamPhase = "STOPPED";
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

function post(url, body) { return apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined }); }
function fmtTime(s) { s = Math.max(0, Math.floor(s || 0)); const m = Math.floor(s / 60), r = s % 60; return `${m}:${String(r).padStart(2, "0")}`; }
function sourceById(id) { return SOURCES.find((s) => s.id === Number(id)); }

// ---------------------------------------------------------------- stream

document.getElementById("r-go-live").addEventListener("click", (e) => streamAction(e.currentTarget, "go-live"));
document.getElementById("r-stop").addEventListener("click", (e) => streamAction(e.currentTarget, "stop", "Stop the live YouTube stream now?"));
document.getElementById("r-restart").addEventListener("click", (e) => streamAction(e.currentTarget, "restart", "Restart the encoder? This briefly interrupts the live stream."));

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
}

let previewTimer = null;
async function pollPreview() {
  if (document.hidden) return;
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
  note.textContent = lv.peak_db > -1 ? "clipping" : lv.rms_db < -50 ? "near silence" : "";
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
    zoomPoll(); zoomTimer = setInterval(zoomPoll, 5000);
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
  try {
    await post(`/api/sources/${s.id}/account`, { account_id: e.target.value || null });
    s.account_id = e.target.value ? Number(e.target.value) : null;
    toast("Account bound - applies on the next switch to this source");
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

async function ytPoll() {
  let st;
  try { st = await apiFetch("/api/youtube/state"); } catch (err) { return; }
  const diag = document.getElementById("yt-diagnosis");
  if (!st.available) {
    document.getElementById("yt-title").textContent = "Browser source not reachable: " + (st.reason || "").replace(/\(.*\)$/, "");
    diag.hidden = true; return;
  }
  document.getElementById("yt-title").textContent = st.has_video ? (st.title || "Playing") : "No player on the current page";
  document.getElementById("yt-quality").textContent = st.quality ? `quality ${st.quality}` : "quality —";
  if (st.has_video) {
    if (!seekDragging) {
      seek.max = Math.max(1, Math.floor(st.duration || 0));
      seek.value = Math.floor(st.current_time || 0);
      document.getElementById("yt-cur").textContent = fmtTime(st.current_time);
    }
    document.getElementById("yt-dur").textContent = st.duration ? fmtTime(st.duration) : "live";
    const muteBtn = document.getElementById("r-yt-mute");
    muteBtn.setAttribute("aria-pressed", String(!!st.muted));
    muteBtn.innerHTML = icon(st.muted ? "volume-x" : "volume-2");
    muteBtn.classList.toggle("btn-danger", !!st.muted); muteBtn.classList.toggle("btn-secondary", !st.muted);
    const vol = document.getElementById("r-yt-volume");
    if (document.activeElement !== vol) vol.value = st.volume ?? 100;
  }
  if (st.diagnosis) {
    diag.hidden = false;
    document.getElementById("yt-diagnosis-msg").textContent = st.diagnosis.message;
    document.getElementById("yt-diagnosis-fix").textContent = st.diagnosis.fix;
    document.getElementById("yt-diagnosis-link").hidden = st.diagnosis.kind === "playback_error";
  } else diag.hidden = true;
}

const ytControl = (action, extra) => async (e) => {
  try { await post("/api/youtube/control", Object.assign({ action }, extra || {})); ytPoll(); }
  catch (err) { toast(err.message, "err"); }
};
document.getElementById("r-yt-play").addEventListener("click", ytControl("play"));
document.getElementById("r-yt-pause").addEventListener("click", ytControl("pause"));
document.getElementById("r-yt-theater").addEventListener("click", ytControl("theater"));
document.getElementById("r-yt-mute").addEventListener("click", (e) => {
  const muted = e.currentTarget.getAttribute("aria-pressed") === "true";
  return ytControl(muted ? "unmute" : "mute")(e);
});
let volumeDebounce = null;
document.getElementById("r-yt-volume").addEventListener("input", (e) => {
  clearTimeout(volumeDebounce);
  const level = Number(e.target.value);
  volumeDebounce = setTimeout(() => post("/api/youtube/control", { action: "volume", level }).catch((err) => toast(err.message, "err")), 200);
});

// library
let ytLinks = Array.from(document.querySelectorAll(".yt-card")).map((el) => ({ id: Number(el.dataset.linkId), url: el.dataset.url, el }));
let ytCurrentIndex = -1;
function renderYtCurrent() { ytLinks.forEach((l, i) => l.el.classList.toggle("is-current", i === ytCurrentIndex)); }

async function ytPlay({ linkId, url, index }) {
  try {
    await post("/api/youtube/play", { link_id: linkId, url });
    if (index != null) ytCurrentIndex = index;
    renderYtCurrent(); toast("Now playing"); announce("YouTube source switched"); ytPoll();
  } catch (err) { toast(err.message, "err"); }
}

document.getElementById("r-yt-list").addEventListener("click", async (e) => {
  const del = e.target.closest(".r-yt-delete");
  if (del) {
    if (!(await confirmDialog("Delete this saved link?", { danger: true }))) return;
    try {
      await apiFetch(`/api/youtube-links/${del.dataset.linkId}`, { method: "DELETE" });
      del.closest(".yt-card").remove();
      ytLinks = ytLinks.filter((l) => l.id !== Number(del.dataset.linkId));
      toast("Link deleted");
    } catch (err) { toast(err.message, "err"); }
    return;
  }
  const play = e.target.closest(".yt-card-play");
  if (!play) return;
  const card = play.closest(".yt-card");
  const index = ytLinks.findIndex((l) => l.id === Number(card.dataset.linkId));
  await withLoading(play, () => ytPlay({ linkId: Number(card.dataset.linkId), index }));
});

document.getElementById("r-yt-save").addEventListener("click", async (e) => {
  const nameEl = document.getElementById("r-yt-name"), urlEl = document.getElementById("r-yt-url");
  const name = nameEl.value.trim(), url = urlEl.value.trim();
  if (!name || !url) { toast("Name and URL are both required", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      const data = await post("/api/youtube-links", { name, url });
      const links = await apiFetch("/api/youtube-links");
      const saved = links.find((l) => l.id === data.id);
      const empty = document.getElementById("r-yt-empty"); if (empty) empty.remove();
      const card = document.createElement("div");
      card.className = "yt-card"; card.setAttribute("role", "listitem"); card.dataset.linkId = data.id; card.dataset.url = url;
      const thumb = saved && saved.thumbnail_url ? `<img src="${saved.thumbnail_url}" alt="" class="yt-thumb">` : `<div class="yt-thumb yt-thumb-empty">${icon("cast")}</div>`;
      card.innerHTML = `<button type="button" class="yt-card-play" aria-label="Play ${name}">${thumb}<span class="yt-card-name"></span></button>` +
        `<button type="button" class="btn btn-ghost btn-icon btn-sm r-yt-delete" data-link-id="${data.id}" aria-label="Delete ${name}">${icon("trash-2", "icon-sm")}</button>`;
      card.querySelector(".yt-card-name").textContent = name;
      document.getElementById("r-yt-list").appendChild(card);
      ytLinks.push({ id: data.id, url, el: card });
      nameEl.value = ""; urlEl.value = "";
      await ytPlay({ linkId: data.id, index: ytLinks.length - 1 });
    } catch (err) { toast(err.message, "err"); }
  });
});
document.getElementById("r-yt-playonce").addEventListener("click", async (e) => {
  const url = document.getElementById("r-yt-url").value.trim();
  if (!url) { toast("Paste a link first", "err"); return; }
  await withLoading(e.currentTarget, () => ytPlay({ url }));
});
document.getElementById("r-yt-prev").addEventListener("click", () => {
  if (!ytLinks.length) return;
  const i = ytCurrentIndex <= 0 ? ytLinks.length - 1 : ytCurrentIndex - 1;
  ytPlay({ linkId: ytLinks[i].id, index: i });
});
document.getElementById("r-yt-next").addEventListener("click", () => {
  if (!ytLinks.length) return;
  const i = ytCurrentIndex >= ytLinks.length - 1 ? 0 : ytCurrentIndex + 1;
  ytPlay({ linkId: ytLinks[i].id, index: i });
});

// ---------------------------------------------------------------- Zoom panel

const ZOOM_STATUS_LABEL = {
  not_joined: "Not joined", connecting: "Connecting…", waiting_room: "In waiting room", not_started: "Host hasn't started",
  in_meeting: "In meeting", ended: "Meeting ended", passcode_required: "Passcode required",
  registration_required: "Registration required", removed: "Removed by host", join_failed: "Could not join", unknown: "Unknown",
};
const ZOOM_STATUS_CLASS = { in_meeting: "badge-active", connecting: "badge-activating", waiting_room: "badge-activating", not_started: "badge-activating",
  ended: "badge-inactive", not_joined: "badge-inactive", unknown: "badge-inactive", passcode_required: "badge-failed", registration_required: "badge-failed", removed: "badge-failed", join_failed: "badge-failed" };

function paintZoomReadback(which, verify) {
  const btn = document.getElementById(which === "mic" ? "z-mic" : "z-camera");
  const iconOn = which === "mic" ? "mic" : "video", iconOff = which === "mic" ? "mic-off" : "eye-off";
  if (verify && verify.available) {
    btn.innerHTML = icon(verify.on ? iconOn : iconOff) + `<span>${which === "mic" ? "Mic" : "Camera"} ${verify.on ? "on" : "off"}</span>`;
    btn.setAttribute("aria-pressed", String(!verify.on));
    btn.classList.toggle("btn-danger", !verify.on); btn.classList.toggle("btn-secondary", verify.on);
  } else {
    btn.innerHTML = icon(iconOn) + `<span>${which === "mic" ? "Mic" : "Camera"} (unverified)</span>`;
    btn.removeAttribute("aria-pressed");
    btn.classList.remove("btn-danger"); btn.classList.add("btn-secondary");
  }
}

async function zoomPoll() {
  let st;
  try { st = await apiFetch("/api/zoom/status"); } catch (err) { return; }
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
    ? `Readback unavailable for ${unverified.join(" and ")}: ${(st.mic && st.mic.reason) || ""} - buttons still send the shortcut.`
    : "Mic/camera state read from Zoom's accessibility labels (best-effort).";
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
function paintZoomMic(verify) {
  if (verify && verify.available) {
    zoomMicBtn.innerHTML = icon(verify.muted ? "mic-off" : "mic"); zoomMicBtn.setAttribute("aria-pressed", String(verify.muted));
    zoomMicHint.textContent = verify.muted ? "Confirmed muted (accessibility check)" : "Confirmed unmuted (accessibility check)";
  } else {
    zoomMicBtn.innerHTML = icon("mic"); zoomMicBtn.removeAttribute("aria-pressed");
    zoomMicHint.textContent = "Sent - can't confirm the resulting state yet";
  }
}
zoomMicBtn.addEventListener("click", async () => {
  await withLoading(zoomMicBtn, async () => {
    try { const d = await post("/api/audio/zoom-mic/toggle"); paintZoomMic(d.verify); toast("Sent mute/unmute to Zoom"); announce("Toggled Zoom mic"); }
    catch (err) { toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- init

apiFetch("/api/audio/stream").then((d) => paintStreamAudio(d.muted, d.volume)).catch(() => {});
pollPreview(); previewTimer = setInterval(pollPreview, 3000);
pollLevel(); levelTimer = setInterval(pollLevel, 1000);
apiFetch("/api/audio/zoom-mic").then(paintZoomMic).catch(() => {});
paintTiles();
showContextPanel();
document.addEventListener("visibilitychange", () => { if (document.hidden) stopPanelPolls(); else showContextPanel(); });
