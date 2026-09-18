// /remote - touch-first quick controls (Part 3). Reuses base.js's
// existing 3s /api/state poll via the "zsdash:state" event it already
// dispatches, rather than starting a second poll loop.

// ---------------------------------------------------------------- stream

document.getElementById("r-go-live").addEventListener("click", (e) => streamAction(e.currentTarget, "go-live"));
document.getElementById("r-stop").addEventListener("click", (e) =>
  streamAction(e.currentTarget, "stop", "Stop the live YouTube stream now?"));
document.getElementById("r-restart").addEventListener("click", (e) =>
  streamAction(e.currentTarget, "restart", "Restart the encoder? This briefly interrupts the live stream."));

async function streamAction(btn, action, confirmMsg) {
  if (confirmMsg && !(await confirmDialog(confirmMsg, { danger: action !== "go-live" }))) return;
  await withLoading(btn, async () => {
    try { await apiFetch(`/api/stream/${action}`, { method: "POST" }); toast(`Stream: ${action.replace("-", " ")} sent`); }
    catch (err) { toast(err.message, "err"); }
  });
}

window.addEventListener("zsdash:state", (e) => {
  const phase = e.detail && e.detail.stream && e.detail.stream.phase;
  if (!phase) return;
  const canStop = phase === "LIVE" || phase === "RECONNECTING";
  document.getElementById("r-go-live").hidden = canStop;
  document.getElementById("r-stop").hidden = !canStop;
});

// ---------------------------------------------------------------- audio: stream mute (optimistic, rolls back on failure)

const streamAudioBtn = document.getElementById("r-stream-audio");

function paintStreamAudio(muted) {
  streamAudioBtn.innerHTML = icon(muted ? "volume-x" : "volume-2");
  streamAudioBtn.setAttribute("aria-pressed", String(muted));
  streamAudioBtn.setAttribute("aria-label", muted ? "Unmute stream audio" : "Mute stream audio");
  streamAudioBtn.classList.toggle("btn-danger", muted);
  streamAudioBtn.classList.toggle("btn-secondary", !muted);
}

async function loadStreamAudioState() {
  try { const data = await apiFetch("/api/audio/stream"); paintStreamAudio(data.muted); }
  catch (err) { /* transient */ }
}

streamAudioBtn.addEventListener("click", async () => {
  const wasMuted = streamAudioBtn.getAttribute("aria-pressed") === "true";
  const nextMuted = !wasMuted;
  paintStreamAudio(nextMuted); // optimistic
  streamAudioBtn.disabled = true;
  try {
    const data = await apiFetch("/api/audio/stream", {
      method: "POST", body: JSON.stringify({ action: nextMuted ? "mute" : "unmute" }),
    });
    paintStreamAudio(data.muted); // commit to the real, read-back state
    toast(data.muted ? "Stream audio muted" : "Stream audio unmuted");
    announce(data.muted ? "Stream audio muted" : "Stream audio unmuted");
  } catch (err) {
    paintStreamAudio(wasMuted); // roll back
    toast(err.message, "err");
  } finally {
    streamAudioBtn.disabled = false;
  }
});

// ---------------------------------------------------------------- audio: Zoom mic (toggle + best-effort verify)

const zoomMicBtn = document.getElementById("r-zoom-mic");
const zoomMicHint = document.getElementById("r-zoom-mic-hint");

function paintZoomMic(verify) {
  if (verify && verify.available) {
    const muted = verify.muted;
    zoomMicBtn.innerHTML = icon(muted ? "mic-off" : "mic");
    zoomMicBtn.setAttribute("aria-pressed", String(muted));
    zoomMicHint.textContent = muted ? "Confirmed muted (via accessibility check)" : "Confirmed unmuted (via accessibility check)";
  } else {
    zoomMicBtn.innerHTML = icon("mic");
    zoomMicBtn.removeAttribute("aria-pressed");
    zoomMicHint.textContent = "Sent - can't confirm the resulting state yet (see Remote notes)";
  }
}

async function loadZoomMicState() {
  try { paintZoomMic(await apiFetch("/api/audio/zoom-mic")); } catch (err) { /* transient */ }
}

zoomMicBtn.addEventListener("click", async () => {
  await withLoading(zoomMicBtn, async () => {
    try {
      const data = await apiFetch("/api/audio/zoom-mic/toggle", { method: "POST" });
      paintZoomMic(data.verify);
      toast("Sent mute/unmute to Zoom");
      announce("Toggled Zoom mic");
    } catch (err) { toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- Zoom leave / rejoin

async function zoomLeave(then, confirmMsg) {
  if (!(await confirmDialog(confirmMsg, { danger: true }))) return;
  try {
    await apiFetch("/api/zoom/leave-with-choice", { method: "POST", body: JSON.stringify({ then }) });
    toast(then === "stop" ? "Left the meeting and stopped the stream" : "Left the meeting - encoder now showing a slate");
  } catch (err) { toast(err.message, "err"); }
}
document.getElementById("r-leave-slate").addEventListener("click", () =>
  zoomLeave("slate", "Leave the meeting? The stream keeps running, showing a plain slate."));
document.getElementById("r-leave-stop").addEventListener("click", () =>
  zoomLeave("stop", "Leave the meeting AND stop the stream now?"));

document.getElementById("r-rejoin").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, async () => {
    try { await apiFetch("/api/zoom/rejoin", { method: "POST" }); toast("Rejoin sent"); }
    catch (err) { toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- YouTube switcher

let ytLinks = Array.from(document.querySelectorAll(".remote-yt-item")).map((el) => ({
  id: Number(el.dataset.linkId), url: el.dataset.url, el,
}));
let ytCurrentIndex = -1;

function renderYtCurrent() {
  ytLinks.forEach((l, i) => l.el.classList.toggle("is-current", i === ytCurrentIndex));
}

async function ytPlay({ linkId, url, index }) {
  try {
    await apiFetch("/api/youtube/play", { method: "POST", body: JSON.stringify({ link_id: linkId, url }) });
    if (index != null) ytCurrentIndex = index;
    renderYtCurrent();
    toast("Now playing");
    announce("YouTube source switched");
  } catch (err) { toast(err.message, "err"); }
}

document.getElementById("r-yt-list").addEventListener("click", async (e) => {
  const del = e.target.closest(".r-yt-delete");
  if (del) {
    e.stopPropagation();
    if (!(await confirmDialog("Delete this saved link?", { danger: true }))) return;
    try {
      await apiFetch(`/api/youtube-links/${del.dataset.linkId}`, { method: "DELETE" });
      del.closest(".remote-yt-item").remove();
      ytLinks = ytLinks.filter((l) => l.id !== Number(del.dataset.linkId));
      toast("Link deleted");
    } catch (err) { toast(err.message, "err"); }
    return;
  }
  const item = e.target.closest(".remote-yt-item");
  if (!item) return;
  const index = ytLinks.findIndex((l) => l.id === Number(item.dataset.linkId));
  await withLoading(item, () => ytPlay({ linkId: Number(item.dataset.linkId), index }));
});

document.getElementById("r-yt-save").addEventListener("click", async (e) => {
  const nameEl = document.getElementById("r-yt-name");
  const urlEl = document.getElementById("r-yt-url");
  const name = nameEl.value.trim();
  const url = urlEl.value.trim();
  if (!name || !url) { toast("Name and URL are both required", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      const data = await apiFetch("/api/youtube-links", { method: "POST", body: JSON.stringify({ name, url }) });
      const empty = document.getElementById("r-yt-empty");
      if (empty) empty.remove();
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "remote-yt-item"; btn.setAttribute("role", "listitem");
      btn.dataset.linkId = data.id; btn.dataset.url = url;
      btn.innerHTML = `${icon("play", "icon-sm")}<span class="remote-yt-item-name">${name}</span>` +
        `<button type="button" class="btn btn-ghost btn-icon btn-sm r-yt-delete" data-link-id="${data.id}" aria-label="Delete ${name}">${icon("trash-2", "icon-sm")}</button>`;
      document.getElementById("r-yt-list").appendChild(btn);
      ytLinks.push({ id: data.id, url, el: btn });
      nameEl.value = ""; urlEl.value = "";
      await ytPlay({ linkId: data.id, index: ytLinks.length - 1 });
    } catch (err) { toast(err.message, "err"); }
  });
});

document.getElementById("r-yt-prev").addEventListener("click", async () => {
  if (!ytLinks.length) return;
  const next = ytCurrentIndex <= 0 ? ytLinks.length - 1 : ytCurrentIndex - 1;
  await ytPlay({ linkId: ytLinks[next].id, index: next });
});
document.getElementById("r-yt-next").addEventListener("click", async () => {
  if (!ytLinks.length) return;
  const next = ytCurrentIndex >= ytLinks.length - 1 ? 0 : ytCurrentIndex + 1;
  await ytPlay({ linkId: ytLinks[next].id, index: next });
});

document.getElementById("r-yt-play").addEventListener("click", async () => {
  try { await apiFetch("/api/youtube/control", { method: "POST", body: JSON.stringify({ action: "play" }) }); }
  catch (err) { toast(err.message, "err"); }
});
document.getElementById("r-yt-pause").addEventListener("click", async () => {
  try { await apiFetch("/api/youtube/control", { method: "POST", body: JSON.stringify({ action: "pause" }) }); }
  catch (err) { toast(err.message, "err"); }
});

let volumeDebounce = null;
document.getElementById("r-yt-volume").addEventListener("input", (e) => {
  clearTimeout(volumeDebounce);
  const level = Number(e.target.value);
  volumeDebounce = setTimeout(async () => {
    try { await apiFetch("/api/youtube/control", { method: "POST", body: JSON.stringify({ action: "volume", level }) }); }
    catch (err) { toast(err.message, "err"); }
  }, 200);
});

// ---------------------------------------------------------------- init

loadStreamAudioState();
loadZoomMicState();
