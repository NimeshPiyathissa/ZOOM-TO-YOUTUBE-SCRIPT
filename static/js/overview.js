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

function renderState(stream) {
  const dot = document.getElementById("hero-dot");
  const text = document.getElementById("hero-text");
  const sub = document.getElementById("hero-sub");
  const heroState = document.getElementById("hero-state");
  const heroCard = document.getElementById("hero-card");
  dot.classList.remove("skeleton");
  text.classList.remove("skeleton");
  heroState.className = "hero-state state-" + stream.state;
  // classList.remove/add (not a className overwrite) so a mid-hover
  // is-hovered/is-tracking class from initCard3DTilt() isn't wiped out
  // by every 3s poll - that would snap the tilt/glow off while the
  // pointer is still sitting on the card.
  Array.from(heroCard.classList).filter((c) => c.startsWith("state-")).forEach((c) => heroCard.classList.remove(c));
  heroCard.classList.add("state-" + stream.state);
  dot.className = "dot-lg dot-" + stream.state;
  text.textContent = stream.state.charAt(0) + stream.state.slice(1).toLowerCase();
  sub.textContent = stream.state === "LIVE"
    ? `Uptime ${fmtUptime(stream.uptime_seconds)} · restarts ${stream.restart_count}`
    : (stream.state === "ERROR" ? "The encoder has failed." : "Not streaming.");
  document.getElementById("btn-go-live").hidden = stream.state === "LIVE";
  document.getElementById("btn-stop-stream").hidden = stream.state !== "LIVE";
  document.getElementById("error-banner").hidden = stream.state !== "ERROR";
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
    badge.className = "badge " + badgeClass(u.active_state);
    badge.innerHTML = `<span class="dot"></span>${label}`;
    document.getElementById(`unit-${u.unit}-uptime`).textContent = fmtUptime(u.uptime_seconds);
    document.getElementById(`unit-${u.unit}-restarts`).textContent = u.restart_count ?? "-";
  }
}

function renderFfmpeg(ff, sourceType, sourceHealth) {
  document.getElementById("ffmpeg-empty").hidden = !!ff;
  document.getElementById("ffmpeg-grid").hidden = !ff;
  document.getElementById("ffmpeg-detail").hidden = !ff;
  document.getElementById("ffmpeg-warning").hidden = !(ff && ff.warning);
  const webpageHealth = document.getElementById("webpage-health");
  if (sourceType === "webpage" && sourceHealth) {
    webpageHealth.hidden = false;
    webpageHealth.innerHTML = sourceHealth.page_loaded
      ? `${icon("check-circle-2", "icon-sm")}Page loaded`
      : `${icon("alert-triangle", "icon-sm")}Page not confirmed loaded yet`;
  } else {
    webpageHealth.hidden = true;
  }
  if (!ff) return;
  document.getElementById("stat-fps").textContent = ff.fps;
  document.getElementById("stat-speed").textContent = ff.speed + "x";
  document.getElementById("stat-bitrate").textContent = Math.round(ff.bitrate_kbps) + "k";
  document.getElementById("ffmpeg-detail").textContent = `frame ${ff.frame} · dropped ${ff.drop} · duplicated ${ff.dup}`;
  renderSparkline(document.getElementById("spark-fps"), pushSparkline("fps", ff.fps));
  renderSparkline(document.getElementById("spark-speed"), pushSparkline("speed", ff.speed));
  renderSparkline(document.getElementById("spark-bitrate"), pushSparkline("bitrate", ff.bitrate_kbps));
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
    renderFfmpeg(data.ffmpeg, data.active_source && data.active_source.type, data.source_health);
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
