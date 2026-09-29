const UNITS = JSON.parse(document.getElementById("units-data").textContent);
const SOURCE_ICON = { zoom: "video", webpage: "globe", direct: "cast" };

document.getElementById("unit-list").innerHTML = UNITS.map(u => `
  <div class="card-3d service-card-3d" id="unit-${u}">
    <div class="service-card-head">
      <span class="service-card-name">${u}</span>
      <span class="badge badge-inactive skeleton" id="unit-${u}-badge">unknown</span>
    </div>
    <div class="service-card-meta">
      <span>Uptime <span class="tnum" id="unit-${u}-uptime">-</span></span>
      <span>Restarts <span class="tnum" id="unit-${u}-restarts">-</span></span>
    </div>
    <div class="service-card-actions">
      <button class="btn btn-ghost btn-icon btn-sm" data-unit="${u}" data-verb="start" aria-label="Start ${u}" data-tooltip="Start">${icon("play", "icon-sm")}</button>
      <button class="btn btn-ghost btn-icon btn-sm" data-unit="${u}" data-verb="stop" aria-label="Stop ${u}" data-tooltip="Stop">${icon("square", "icon-sm")}</button>
      <button class="btn btn-ghost btn-icon btn-sm" data-unit="${u}" data-verb="restart" aria-label="Restart ${u}" data-tooltip="Restart">${icon("rotate-cw", "icon-sm")}</button>
    </div>
  </div>`).join("");

document.getElementById("unit-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-unit]");
  if (!btn) return;
  await withLoading(btn, async () => {
    try {
      await apiFetch(`/api/units/${btn.dataset.unit}/${btn.dataset.verb}`, { method: "POST" });
      toast(`${btn.dataset.unit}: ${btn.dataset.verb} sent`);
    } catch (err) { toast(err.message, "err"); }
  });
});

async function streamAction(btn, action, confirmMsg) {
  if (confirmMsg && !(await confirmDialog(confirmMsg, { danger: action === "stop" }))) return;
  await withLoading(btn, async () => {
    try { await apiFetch(`/api/stream/${action}`, { method: "POST" }); toast(`Stream: ${action} sent`); }
    catch (err) { toast(err.message, "err"); }
  });
}
document.getElementById("btn-go-live").onclick = (e) => streamAction(e.currentTarget, "go-live");
document.getElementById("btn-stop-stream").onclick = (e) => streamAction(e.currentTarget, "stop", "Stop the live YouTube stream now?");
document.getElementById("btn-restart-stream").onclick = (e) => streamAction(e.currentTarget, "restart", "Restart the encoder? This will briefly interrupt the live stream.");

document.getElementById("hero-source-copy").addEventListener("click", async () => {
  const url = document.getElementById("hero-source-url").dataset.full || "";
  if (!url) return;
  try { await navigator.clipboard.writeText(url); toast("URL copied"); }
  catch (err) { toast("Could not copy", "err"); }
});

// ---------------------------------------------------------------- render

// CSS still keys off a small closed set of state-* / dot-* class names
// (state-LIVE, state-ERROR, ...) - FAILED reuses the existing ERROR
// visuals (same meaning, renamed), the others (STOPPED/STARTING/
// RECONNECTING) each have their own rule in layout.css / cards-3d.css.
function _cssStateFor(phase) {
  return phase === "FAILED" ? "ERROR" : phase;
}

const STATE_LABEL = {
  STOPPED: "Stopped", STARTING: "Starting…", LIVE: "Live",
  RECONNECTING: "Reconnecting…", FAILED: "Failed",
};

function renderState(stream) {
  const dot = document.getElementById("hero-dot");
  const text = document.getElementById("hero-text");
  const sub = document.getElementById("hero-sub");
  const heroState = document.getElementById("hero-state");
  const heroCard = document.getElementById("hero-card");
  const cssState = _cssStateFor(stream.phase);
  dot.classList.remove("skeleton");
  text.classList.remove("skeleton");
  heroState.className = "hero-state state-" + cssState;
  // classList.remove/add (not a className overwrite) so a mid-hover
  // is-hovered/is-tracking class from initCard3DTilt() isn't wiped out
  // by every 3s poll - that would snap the tilt/glow off while the
  // pointer is still sitting on the card.
  Array.from(heroCard.classList).filter((c) => c.startsWith("state-")).forEach((c) => heroCard.classList.remove(c));
  heroCard.classList.add("state-" + cssState);
  dot.className = "dot-lg dot-" + cssState;
  text.textContent = STATE_LABEL[stream.phase] || stream.phase;

  if (stream.phase === "LIVE") {
    sub.textContent = `Uptime ${fmtUptime(stream.uptime_seconds)} · restarts ${stream.restart_count}`;
  } else if (stream.phase === "RECONNECTING") {
    sub.textContent = `The encoder keeps crash-looping (restart ${stream.restart_count}) — not live right now.`;
  } else if (stream.phase === "STARTING") {
    sub.textContent = "Starting the encoder…";
  } else if (stream.phase === "FAILED") {
    sub.textContent = `Gave up after ${stream.restart_count} restarts.`;
  } else {
    sub.textContent = "Not streaming.";
  }

  document.getElementById("btn-go-live").hidden = stream.phase === "LIVE" || stream.phase === "RECONNECTING";
  document.getElementById("btn-stop-stream").hidden = stream.phase !== "LIVE" && stream.phase !== "RECONNECTING";

  const banner = document.getElementById("error-banner");
  banner.hidden = stream.phase !== "FAILED";
  if (stream.phase === "FAILED") {
    document.getElementById("error-banner-headline").textContent =
      `The encoder has failed after ${stream.restart_count} restarts.` +
      (stream.cause ? ` Likely cause: ${stream.cause}.` : "");
    document.getElementById("error-banner-detail").textContent =
      stream.last_error ? stream.last_error : "";
  }

  renderRestartRate(stream.restarts_last_5min);
}

// Persistent, always-visible restart/crash-rate indicator (not just when
// alarming) - the whole point is that a climbing count can't be missed
// the way ~22,000 silent restarts were during the incident this exists
// to catch. Alarms (red, pulsing) above 3 restarts in 5 minutes.
const RESTART_RATE_ALARM_THRESHOLD = 3;

function renderRestartRate(count) {
  const el = document.getElementById("restart-rate-badge");
  const label = document.getElementById("restart-rate-text");
  if (count == null) { el.hidden = true; return; }
  el.hidden = false;
  const alarming = count > RESTART_RATE_ALARM_THRESHOLD;
  el.className = "badge restart-rate-badge " + (alarming ? "badge-failed is-alarm" : "badge-inactive");
  label.textContent = `${count} restart${count === 1 ? "" : "s"} / 5 min`;
  el.setAttribute("aria-label", `${count} restarts in the last 5 minutes` + (alarming ? " - crash looping" : ""));
}

function renderActiveSource(source) {
  const wrap = document.getElementById("hero-source");
  if (!source) { wrap.hidden = true; return; }
  wrap.hidden = false;
  document.getElementById("hero-source-icon").innerHTML = icon(SOURCE_ICON[source.type] || "link", "icon-sm");
  document.getElementById("hero-source-name").textContent = source.name;
  const urlEl = document.getElementById("hero-source-url");
  urlEl.textContent = source.url_truncated;
  urlEl.dataset.full = source.url;
}

function renderUnits(units) {
  for (const u of units) {
    const badge = document.getElementById(`unit-${u.unit}-badge`);
    if (!badge) continue;
    badge.classList.remove("skeleton");
    const label = u.active_state + (u.sub_state ? ` (${u.sub_state})` : "");
    // Colored by the same derived `phase` the hero uses (falls back to
    // raw-active_state coloring only if a unit somehow has no phase,
    // e.g. the unit_show() error-fallback shape) - never a second,
    // independently-computed guess.
    badge.className = "badge " + (u.phase ? phaseBadgeClass(u.phase) : badgeClass(u.active_state));
    badge.innerHTML = `<span class="dot"></span>${label}`;
    document.getElementById(`unit-${u.unit}-uptime`).textContent = fmtUptime(u.uptime_seconds);
    document.getElementById(`unit-${u.unit}-restarts`).textContent = u.restart_count ?? "-";
  }
}

const FFMPEG_EMPTY_TEXT = {
  STOPPED: "No data yet — the stream isn't running.",
  STARTING: "Starting the encoder…",
  RECONNECTING: "Crash-looping — not live right now.",
  FAILED: "The encoder has failed. See above for details.",
};

function renderFfmpeg(ff, phase, sourceType, sourceHealth) {
  const hasData = !!ff;
  document.getElementById("ffmpeg-empty").hidden = hasData;
  document.getElementById("ffmpeg-empty").querySelector(".empty-sub").textContent =
    FFMPEG_EMPTY_TEXT[phase] || FFMPEG_EMPTY_TEXT.STOPPED;
  document.getElementById("ffmpeg-grid").hidden = !hasData;
  document.getElementById("ffmpeg-detail").hidden = !hasData;
  document.getElementById("ffmpeg-warning").hidden = !(ff && ff.warning);
  const updated = document.getElementById("ffmpeg-updated");

  const webpageHealth = document.getElementById("webpage-health");
  if (sourceType === "webpage" && sourceHealth) {
    webpageHealth.hidden = false;
    webpageHealth.innerHTML = sourceHealth.page_loaded
      ? `${icon("check-circle-2", "icon-sm")}Page loaded`
      : `${icon("alert-triangle", "icon-sm")}Page not confirmed loaded yet`;
  } else {
    webpageHealth.hidden = true;
  }

  if (!hasData) {
    // Explicitly reset to "-" rather than leaving whatever was last
    // rendered on screen - the whole bug this fixes was stale numbers
    // staying visible after the run that produced them had died.
    document.getElementById("stat-fps").textContent = "-";
    document.getElementById("stat-speed").textContent = "-";
    document.getElementById("stat-bitrate").textContent = "-";
    updated.hidden = true;
    return;
  }

  document.getElementById("stat-fps").textContent = ff.fps;
  document.getElementById("stat-speed").textContent = ff.speed + "x";
  document.getElementById("stat-bitrate").textContent = Math.round(ff.bitrate_kbps) + "k";
  document.getElementById("ffmpeg-detail").textContent = `frame ${ff.frame} · dropped ${ff.drop} · duplicated ${ff.dup}`;
  renderSparkline(document.getElementById("spark-fps"), pushSparkline("fps", ff.fps));
  renderSparkline(document.getElementById("spark-speed"), pushSparkline("speed", ff.speed));
  renderSparkline(document.getElementById("spark-bitrate"), pushSparkline("bitrate", ff.bitrate_kbps));

  updated.hidden = false;
  updated.textContent = "updated " + fmtAgo(ff.age_seconds);
  // Grey out (rather than just showing the text) once data is a few
  // seconds old, so a viewer doesn't have to read fine print to notice
  // the numbers stopped moving.
  document.getElementById("ffmpeg-grid").classList.toggle("is-stale", ff.age_seconds > 5);
}

function renderSystem(sys) {
  document.getElementById("cpu-avg").childNodes[0].textContent = sys.cpu_avg;
  renderCpuCores(document.getElementById("cpu-cores"), sys.cpu_per_core);
  document.getElementById("mem-big").textContent = `${sys.mem_percent.toFixed(0)}%`;
  document.getElementById("mem-sub").textContent = `${sys.mem_used_gb} / ${sys.mem_total_gb} GB`;
  document.getElementById("disk-big").textContent = `${sys.disk_percent.toFixed(0)}%`;
  document.getElementById("disk-sub").textContent = `${sys.disk_used_gb} / ${sys.disk_total_gb} GB`;
  document.getElementById("net-big").textContent = sys.upload_kbps != null ? `${(sys.upload_kbps/1000).toFixed(2)} Mbps` : "-";
  document.getElementById("load-sub").textContent = `load ${sys.load.join(" / ")}`;
  renderSparkline(document.getElementById("spark-mem"), pushSparkline("mem", sys.mem_percent));
  if (sys.upload_kbps != null) renderSparkline(document.getElementById("spark-net"), pushSparkline("net", sys.upload_kbps));
}

async function renderAccount() {
  const el = document.getElementById("account-status");
  try {
    const data = await apiFetch("/api/zoom/account");
    el.textContent = data.confirmed_signed_in ? `Signed in as ${data.label || "(unlabeled)"}` : "Signed out (guest join)";
  } catch (err) { /* transient */ }
}

async function pollState() {
  try {
    const data = await apiFetch("/api/state");
    renderState(data.stream);
    renderActiveSource(data.active_source);
    renderUnits(data.units);
    renderFfmpeg(data.ffmpeg, data.stream.phase, data.active_source && data.active_source.type, data.source_health);
    renderSystem(data.system);
  } catch (err) { /* transient errors are fine on a poll loop */ }
}

async function pollPreview() {
  if (document.hidden) return;
  const img = document.getElementById("preview-img");
  const placeholder = document.getElementById("preview-placeholder");
  try {
    const res = await fetch(`/api/preview.jpg?t=${Date.now()}`, { credentials: "same-origin" });
    if (!res.ok) throw new Error("no preview");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const old = img.src;
    img.src = url;
    img.hidden = false;
    placeholder.hidden = true;
    if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
  } catch (err) {
    img.hidden = true;
    placeholder.hidden = false;
  }
}

// ---------------------------------------------------------------- premium 3D card interaction
//
// Pointer-driven tilt + light sheen, desktop-only: gated on (hover:
// hover) and (pointer: fine) so touch devices never attach these
// listeners at all, and on prefers-reduced-motion. Only transform/
// box-shadow change (both GPU-friendly), driven through CSS custom
// properties so cards-3d.css owns the actual visual values.

function initCard3DTilt() {
  const canTilt = window.matchMedia("(hover: hover) and (pointer: fine)").matches
    && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!canTilt) return;

  const MAX_TILT_DEG = 5;
  document.querySelectorAll(".card-3d").forEach((card) => {
    function onMove(e) {
      const rect = card.getBoundingClientRect();
      const px = (e.clientX - rect.left) / rect.width;
      const py = (e.clientY - rect.top) / rect.height;
      const tiltY = (px - 0.5) * 2 * MAX_TILT_DEG;
      const tiltX = (0.5 - py) * 2 * MAX_TILT_DEG;
      card.style.setProperty("--tilt-x", `${tiltX.toFixed(2)}deg`);
      card.style.setProperty("--tilt-y", `${tiltY.toFixed(2)}deg`);
      card.style.setProperty("--mx", `${(px * 100).toFixed(1)}%`);
      card.style.setProperty("--my", `${(py * 100).toFixed(1)}%`);
    }
    card.addEventListener("pointerenter", (e) => {
      if (e.pointerType !== "mouse") return;
      card.classList.add("is-tracking", "is-hovered");
      card.style.setProperty("--lift", "-4px");
    });
    card.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") onMove(e); });
    card.addEventListener("pointerleave", (e) => {
      if (e.pointerType !== "mouse") return;
      card.classList.remove("is-tracking", "is-hovered");
      card.style.setProperty("--tilt-x", "0deg");
      card.style.setProperty("--tilt-y", "0deg");
      card.style.setProperty("--lift", "0px");
      card.style.removeProperty("--press-scale");
    });
    card.addEventListener("pointerdown", (e) => { if (e.pointerType === "mouse") card.style.setProperty("--press-scale", "0.985"); });
    card.addEventListener("pointerup", (e) => { if (e.pointerType === "mouse") card.style.setProperty("--press-scale", "1"); });
  });
}

pollState();
pollPreview();
renderAccount();
initCard3DTilt();
setInterval(pollState, 3000);
setInterval(pollPreview, 3000);
setInterval(renderAccount, 30000);

// ---------------------------------------------------------------- local recording controls
const btnOverviewRecord = document.getElementById("btn-overview-record");
const overviewRecText = document.getElementById("overview-rec-text");
const overviewRecIcon = document.getElementById("overview-rec-icon");
const overviewRecPill = document.getElementById("overview-rec-pill");
const overviewRecTimer = document.getElementById("overview-rec-timer");

let isRecordingOverview = false;

async function pollOverviewRecording() {
  if (!btnOverviewRecord) return;
  try {
    const data = await apiFetch("/api/record");
    isRecordingOverview = !!data.recording;

    if (isRecordingOverview) {
      btnOverviewRecord.className = "btn btn-rec-active btn-lg";
      if (overviewRecIcon) overviewRecIcon.textContent = "⏹";
      if (overviewRecText) overviewRecText.textContent = "Stop Recording";
      if (overviewRecPill) overviewRecPill.style.display = "inline-flex";
      if (overviewRecTimer) {
        const dur = parseInt(data.duration, 10) || 0;
        const h = String(Math.floor(dur / 3600)).padStart(2, "0");
        const m = String(Math.floor((dur % 3600) / 60)).padStart(2, "0");
        const s = String(dur % 60).padStart(2, "0");
        overviewRecTimer.textContent = `${h}:${m}:${s}`;
      }
    } else {
      btnOverviewRecord.className = "btn btn-rec btn-lg";
      if (overviewRecIcon) overviewRecIcon.textContent = "⏺";
      if (overviewRecText) overviewRecText.textContent = "Start Recording";
      if (overviewRecPill) overviewRecPill.style.display = "none";
    }
  } catch (err) {
    // Ignore polling errors
  }
}

if (btnOverviewRecord) {
  btnOverviewRecord.addEventListener("click", async () => {
    const action = isRecordingOverview ? "stop" : "start";
    await withLoading(btnOverviewRecord, async () => {
      try {
        const res = await apiFetch("/api/record", {
          method: "POST",
          body: JSON.stringify({ action }),
        });
        await pollOverviewRecording();
        toast(action === "start" ? "Recording started" : "Recording stopped", "ok");
      } catch (err) {
        toast(`Recording ${action} failed: ${err.message}`, "err");
      }
    });
  });
}

pollOverviewRecording();
setInterval(pollOverviewRecording, 2000);

