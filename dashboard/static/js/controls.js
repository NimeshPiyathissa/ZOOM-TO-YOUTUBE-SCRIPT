const UNITS = JSON.parse(document.getElementById("units-data").textContent);

document.getElementById("unit-list").innerHTML = UNITS.map(u => `
  <div class="service-row" id="unit-${u}">
    <span class="service-name">${u}</span>
    <span class="badge badge-inactive skeleton" id="unit-${u}-badge">unknown</span>
    <div class="service-meta">
      <span>Uptime <span class="tnum" id="unit-${u}-uptime">-</span></span>
      <span>Restarts <span class="tnum" id="unit-${u}-restarts">-</span></span>
    </div>
    <div class="service-actions">
      <button class="btn btn-ghost btn-icon btn-sm" data-unit="${u}" data-verb="start" aria-label="Start ${u}" data-tooltip="Start">${icon("play", "icon-sm")}</button>
      <button class="btn btn-ghost btn-icon btn-sm" data-unit="${u}" data-verb="stop" aria-label="Stop ${u}" data-tooltip="Stop">${icon("square", "icon-sm")}</button>
      <button class="btn btn-ghost btn-icon btn-sm" data-unit="${u}" data-verb="restart" aria-label="Restart ${u}" data-tooltip="Restart">${icon("rotate-cw", "icon-sm")}</button>
    </div>
  </div>`).join("");

async function simpleAction(btn, url, okMsg, confirmMsg, danger) {
  if (confirmMsg && !(await confirmDialog(confirmMsg, { danger }))) return;
  await withLoading(btn, async () => {
    try { await apiFetch(url, { method: "POST" }); toast(okMsg); }
    catch (err) { toast(err.message, "err"); }
  });
}

document.getElementById("btn-join").onclick = (e) => simpleAction(e.currentTarget, "/api/zoom/join", "Joining meeting...");
document.getElementById("btn-leave").onclick = (e) => simpleAction(e.currentTarget, "/api/zoom/leave", "Left meeting", "Leave the Zoom meeting now?", true);
document.getElementById("btn-rejoin").onclick = (e) => simpleAction(e.currentTarget, "/api/zoom/rejoin", "Rejoining...");
document.getElementById("btn-pipeline-restart").onclick = (e) =>
  simpleAction(e.currentTarget, "/api/pipeline/restart", "Pipeline restart sent", "Restart the entire pipeline in order? This will interrupt any live stream.", true);

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

document.getElementById("btn-test-recording").onclick = async (e) => {
  const btn = e.currentTarget;
  toast("Recording 60s test clip... this will take about a minute.");
  await withLoading(btn, async () => {
    try {
      const res = await apiFetch("/api/test-recording", { method: "POST" });
      toast(res.ok ? "Test recording complete - use Download latest." : "Test recording failed, see Logs.", res.ok ? "ok" : "err");
    } catch (err) { toast(err.message, "err"); }
  });
};

document.getElementById("btn-reboot").onclick = async () => {
  const ok = await confirmDialog(
    "This immediately reboots the VPS, ending any live stream and dropping the meeting. This cannot be undone.",
    { danger: true, confirmText: "Reboot now", requireText: "REBOOT" });
  if (!ok) return;
  try {
    await apiFetch("/api/reboot", { method: "POST", body: JSON.stringify({ confirm: "REBOOT" }) });
    toast("Rebooting - the dashboard will be unreachable for a minute or two.");
  } catch (err) { toast(err.message, "err"); }
};

// ---------------------------------------------------------------- sources (Change 1)

const sourceIcon = { zoom: "video", webpage: "globe", direct: "cast" };
const quickSource = document.getElementById("quick-source");
const switchBtn = document.getElementById("btn-switch-source");

if (switchBtn) {
  switchBtn.onclick = async () => {
    if (!quickSource.value) { toast("Pick a source first", "err"); return; }
    const label = quickSource.options[quickSource.selectedIndex].text;
    if (!(await confirmDialog(`Switch to "${label}"? The encoder restarts briefly (a few seconds of buffering on YouTube's side).`))) return;
    await withLoading(switchBtn, async () => {
      try {
        await apiFetch(`/api/sources/${quickSource.value}/switch`, { method: "POST" });
        toast(`Switched to ${label}`);
      } catch (err) { toast(err.message, "err"); }
    });
  };
}

function renderActiveSource(data) {
  const badge = document.getElementById("active-source-badge");
  const urlEl = document.getElementById("active-source-url");
  if (!badge) return;
  badge.classList.remove("skeleton");
  if (data.active_source) {
    const s = data.active_source;
    badge.className = "badge badge-active";
    badge.innerHTML = `${icon(sourceIcon[s.type] || "link", "icon-sm")}${s.name} (${s.type})`;
    urlEl.textContent = s.url_truncated;
  } else {
    badge.className = "badge badge-inactive";
    badge.innerHTML = "No active source";
    urlEl.textContent = "";
  }
}

function renderUnits(units) {
  for (const u of units) {
    const badge = document.getElementById(`unit-${u.unit}-badge`);
    if (!badge) continue;
    badge.classList.remove("skeleton");
    badge.className = "badge " + badgeClass(u.active_state);
    badge.innerHTML = `<span class="dot"></span>${u.active_state}${u.sub_state ? ` (${u.sub_state})` : ""}`;
    document.getElementById(`unit-${u.unit}-uptime`).textContent = fmtUptime(u.uptime_seconds);
    document.getElementById(`unit-${u.unit}-restarts`).textContent = u.restart_count ?? "-";
  }
}

async function poll() {
  try {
    const data = await apiFetch("/api/state");
    renderUnits(data.units);
    renderActiveSource(data);
  } catch (err) { /* transient */ }
}

poll();
setInterval(poll, 3000);
