// Shared schedule manager - renders into any page's #schedule-manager
// container (the /schedule page and the Live Studio "Schedule" card both
// use this same file against the same /api/schedules API, so they can
// never show different data). See app/scheduler.py's "rich schedules"
// section for the engine behind this, and app/main.py's
// /api/schedules* routes for the API this talks to.

(() => {
  const container = document.getElementById("schedule-manager");
  if (!container) return;

  const sourcesEl = document.getElementById("schedule-sources-data");
  const ALL_SOURCES = sourcesEl ? JSON.parse(sourcesEl.textContent || "[]") : [];
  const SOURCES = ALL_SOURCES.filter((s) => s.type === "zoom" || s.type === "webpage");

  const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const STATE_LABELS = {
    idle: "Waiting for its window", joining: "Joining…", joined: "Joined, waiting to go live",
    live: "Live", done: "Done", skipped: "Skipped", failed: "Failed",
    manually_stopped: "Stopped manually",
  };
  const STATE_BADGE = {
    idle: "badge-inactive", joining: "badge-warning", joined: "badge-warning",
    live: "badge-live", done: "badge-inactive", skipped: "badge-inactive",
    failed: "badge-error", manually_stopped: "badge-inactive",
  };

  let schedules = [];
  let ntpSynced = null;
  let serverOffsetMs = 0; // serverTime - Date.now(), applied so the countdown doesn't drift from client-clock skew
  let editingId = null;
  let lastFetchFailed = false;

  const post = (url, body) => apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined });

  function fmtDuration(seconds) {
    if (seconds == null) return "";
    seconds = Math.max(0, Math.round(seconds));
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  function fmtClock(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("en-GB", {
      timeZone: "Asia/Colombo", weekday: "short", day: "2-digit", month: "short",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
  }

  function repeatSummary(s) {
    if (s.repeat_mode === "once") return `Once on ${s.start_date}`;
    if (s.repeat_mode === "daily") return "Daily";
    if (s.repeat_mode === "weekdays") return "Weekdays";
    if (s.repeat_mode === "custom") return (s.active_days || []).map((d) => DAY_LABELS[d]).join(", ") || "Custom";
    return s.repeat_mode;
  }

  function sourceOptionsHtml(selectedId) {
    if (SOURCES.length === 0) {
      return `<option value="">No saved Zoom meetings or web sources yet</option>`;
    }
    return SOURCES.map((s) => `<option value="${s.id}" ${String(s.id) === String(selectedId) ? "selected" : ""}>${escapeHtml(s.name)} (${s.type})</option>`).join("");
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // -------------------------------------------------------------- rendering

  function renderShell() {
    container.innerHTML = `
      <div id="sched-ntp-banner" class="banner banner-warning" hidden>
        ${icon("alert-triangle", "icon-sm")}
        <span>This server's clock is not NTP-synced - scheduled run times may be inaccurate until it is.</span>
      </div>
      <div id="sched-list"></div>
      <details id="sched-form-details">
        <summary class="btn btn-primary" id="sched-form-summary">${icon("plus", "icon-sm")}Add schedule</summary>
        <form id="sched-form" class="form-grid mt-3">
          <input type="hidden" id="sf-id">
          <div class="field"><label class="label">Label (optional)</label>
            <input class="input" id="sf-label" placeholder="e.g. Sunday morning service"></div>
          <div class="field"><label class="label">Source</label>
            <select class="input" id="sf-source" required>${sourceOptionsHtml(null)}</select></div>
          <div class="field grid-col-full"><label class="label">Repeat</label>
            <div class="segmented" id="sf-repeat-seg" role="group" aria-label="Repeat">
              <button type="button" class="seg-btn is-active" data-value="once">Once</button>
              <button type="button" class="seg-btn" data-value="daily">Daily</button>
              <button type="button" class="seg-btn" data-value="weekdays">Weekdays</button>
              <button type="button" class="seg-btn" data-value="custom">Custom</button>
            </div>
          </div>
          <div class="field grid-col-full" id="sf-days-field" hidden>
            <label class="label">Days</label>
            <div class="segmented" id="sf-days-seg" role="group" aria-label="Custom days">
              ${DAY_LABELS.map((d, i) => `<button type="button" class="seg-btn" data-value="${i}">${d}</button>`).join("")}
            </div>
          </div>
          <div class="field"><label class="label" id="sf-date-label">Date</label>
            <input class="input" id="sf-date" type="date"></div>
          <div class="field"><label class="label">Start time</label>
            <input class="input" id="sf-start" type="time" value="08:00" required></div>
          <div class="field"><label class="label">End time</label>
            <input class="input" id="sf-end" type="time" value="14:00" required></div>
          <div class="field">
            <label class="label">&nbsp;</label>
            <label class="switch-row"><span class="switch"><input type="checkbox" id="sf-overnight"><span class="track"></span></span>
              <span class="switch-label">Ends next day (overnight run)</span></label>
          </div>
          <div class="field"><label class="label">Join lead time (minutes)</label>
            <input class="input" id="sf-lead" type="number" min="0" max="120" value="5"></div>
          <div class="field">
            <label class="label">&nbsp;</label>
            <label class="switch-row"><span class="switch"><input type="checkbox" id="sf-keep-open"><span class="track"></span></span>
              <span class="switch-label">Keep meeting open at end (don't leave)</span></label>
          </div>
          <div class="grid-col-full btn-row">
            <button type="submit" class="btn btn-primary" id="sf-save">${icon("check", "icon-sm")}Save schedule</button>
            <button type="button" class="btn btn-ghost" id="sf-cancel">Cancel</button>
          </div>
        </form>
      </details>
    `;
    wireForm();
  }

  function renderList() {
    const listEl = document.getElementById("sched-list");
    if (!listEl) return;
    const ntpBanner = document.getElementById("sched-ntp-banner");
    if (ntpBanner) ntpBanner.hidden = ntpSynced !== false;

    if (lastFetchFailed && schedules.length === 0) {
      listEl.innerHTML = `<div class="empty-state">${icon("alert-triangle")}<div class="empty-title">Couldn't load schedules</div></div>`;
      return;
    }
    if (schedules.length === 0) {
      listEl.innerHTML = `<div class="empty-state">${icon("calendar-clock")}<div class="empty-title">No schedules yet</div><div class="empty-sub">Add one below to join early, go live on time, and stop automatically.</div></div>`;
      return;
    }
    const nowMs = Date.now() + serverOffsetMs;
    listEl.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Schedule</th><th>Window</th><th>Repeat</th><th>Status</th><th>Next run (Asia/Colombo)</th><th></th></tr></thead>
      <tbody>${schedules.map((s) => renderRow(s, nowMs)).join("")}</tbody>
    </table></div>`;
    wireRowButtons();
  }

  function renderRow(s, nowMs) {
    const badge = STATE_BADGE[s.state] || "badge-inactive";
    const stateLabel = s.state_detail || STATE_LABELS[s.state] || s.state;
    let nextRunText = "—";
    if (!s.enabled) {
      nextRunText = "Disabled";
    } else if (s.countdown_seconds != null && s.next_run) {
      const secsLeft = Math.max(0, Math.round((new Date(s.next_run).getTime() - nowMs) / 1000));
      nextRunText = secsLeft > 0
        ? `in ${fmtDuration(secsLeft)} <span class="hint">(${fmtClock(s.next_run)})</span>`
        : `<span class="badge badge-live">Active now</span>`;
    }
    const warn = (s.warnings || []).length
      ? `<span class="hint" data-tooltip="${escapeHtml(s.warnings.join(" "))}">${icon("alert-triangle", "icon-sm")}</span>` : "";
    return `<tr data-id="${s.id}">
      <td><strong>${escapeHtml(s.label || s.source_name || "Schedule #" + s.id)}</strong>
        <div class="hint">${escapeHtml(s.source_name || "(source deleted)")} ${warn}</div></td>
      <td class="mono">${s.start_time}–${s.end_time}${s.overnight ? " <span class=\"hint\">(+1d)</span>" : ""}</td>
      <td>${repeatSummary(s)}</td>
      <td><span class="badge ${badge}">${escapeHtml(stateLabel)}</span></td>
      <td>${nextRunText}</td>
      <td class="actions-cell">
        <label class="switch" data-tooltip="${s.enabled ? "Disable" : "Enable"}">
          <input type="checkbox" class="sched-enable-toggle" ${s.enabled ? "checked" : ""}><span class="track"></span>
        </label>
        <button class="btn btn-ghost btn-icon btn-sm sched-run-now" data-tooltip="Run now" ${s.enabled ? "" : "disabled"}>${icon("play", "icon-sm")}</button>
        <button class="btn btn-ghost btn-icon btn-sm sched-skip-next" data-tooltip="Skip next run" ${s.enabled && !s.skip_next ? "" : "disabled"}>${icon("skip-forward", "icon-sm")}</button>
        <button class="btn btn-ghost btn-icon btn-sm sched-edit" data-tooltip="Edit">${icon("pencil", "icon-sm")}</button>
        <button class="btn btn-ghost btn-icon btn-sm sched-delete" data-tooltip="Delete">${icon("trash-2", "icon-sm")}</button>
      </td>
    </tr>`;
  }

  function wireRowButtons() {
    document.querySelectorAll(".sched-enable-toggle").forEach((el) => {
      el.addEventListener("change", async () => {
        const id = el.closest("tr").dataset.id;
        try { await post(`/api/schedules/${id}/${el.checked ? "enable" : "disable"}`); await refresh(); }
        catch (err) { toast(err.message, "err"); el.checked = !el.checked; }
      });
    });
    document.querySelectorAll(".sched-run-now").forEach((el) => {
      el.addEventListener("click", async () => {
        const id = el.closest("tr").dataset.id;
        try { await post(`/api/schedules/${id}/run-now`); toast("Starting now"); await refresh(); }
        catch (err) { toast(err.message, "err"); }
      });
    });
    document.querySelectorAll(".sched-skip-next").forEach((el) => {
      el.addEventListener("click", async () => {
        const id = el.closest("tr").dataset.id;
        try { await post(`/api/schedules/${id}/skip-next`); toast("Next run will be skipped"); await refresh(); }
        catch (err) { toast(err.message, "err"); }
      });
    });
    document.querySelectorAll(".sched-delete").forEach((el) => {
      el.addEventListener("click", async () => {
        const id = el.closest("tr").dataset.id;
        if (!(await confirmDialog("Delete this schedule?", { danger: true }))) return;
        try { await apiFetch(`/api/schedules/${id}`, { method: "DELETE" }); await refresh(); }
        catch (err) { toast(err.message, "err"); }
      });
    });
    document.querySelectorAll(".sched-edit").forEach((el) => {
      el.addEventListener("click", () => {
        const id = el.closest("tr").dataset.id;
        const s = schedules.find((x) => String(x.id) === id);
        if (s) openForEdit(s);
      });
    });
  }

  // -------------------------------------------------------------- form

  function wireForm() {
    const details = document.getElementById("sched-form-details");
    const repeatSeg = document.getElementById("sf-repeat-seg");
    const daysSeg = document.getElementById("sf-days-seg");
    const daysField = document.getElementById("sf-days-field");
    const dateLabel = document.getElementById("sf-date-label");

    function setRepeat(mode) {
      repeatSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.value === mode));
      daysField.hidden = mode !== "custom";
      dateLabel.textContent = mode === "once" ? "Date" : "Starts on";
    }
    repeatSeg.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => setRepeat(b.dataset.value)));
    daysSeg.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => b.classList.toggle("is-active")));

    document.getElementById("sf-cancel").addEventListener("click", () => {
      resetForm();
      details.open = false;
    });

    document.getElementById("sched-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const repeatMode = repeatSeg.querySelector(".seg-btn.is-active")?.dataset.value || "once";
      const customDays = [...daysSeg.querySelectorAll(".seg-btn.is-active")].map((b) => Number(b.dataset.value));
      const body = {
        label: document.getElementById("sf-label").value,
        source_id: Number(document.getElementById("sf-source").value) || null,
        repeat_mode: repeatMode,
        start_date: document.getElementById("sf-date").value || null,
        custom_days: customDays,
        start_time: document.getElementById("sf-start").value,
        end_time: document.getElementById("sf-end").value,
        overnight: document.getElementById("sf-overnight").checked,
        join_lead_minutes: Number(document.getElementById("sf-lead").value) || 0,
        keep_meeting_open: document.getElementById("sf-keep-open").checked,
      };
      const id = document.getElementById("sf-id").value;
      const btn = document.getElementById("sf-save");
      await withLoading(btn, async () => {
        try {
          if (id) await apiFetch(`/api/schedules/${id}`, { method: "PUT", body: JSON.stringify(body) });
          else await post("/api/schedules", body);
          toast(id ? "Schedule updated" : "Schedule added");
          resetForm();
          details.open = false;
          await refresh();
        } catch (err) { toast(err.message, "err"); }
      });
    });

    setRepeat("once");
  }

  function resetForm() {
    editingId = null;
    document.getElementById("sf-id").value = "";
    document.getElementById("sf-label").value = "";
    document.getElementById("sf-source").value = "";
    document.getElementById("sf-date").value = "";
    document.getElementById("sf-start").value = "08:00";
    document.getElementById("sf-end").value = "14:00";
    document.getElementById("sf-overnight").checked = false;
    document.getElementById("sf-lead").value = "5";
    document.getElementById("sf-keep-open").checked = false;
    document.getElementById("sf-repeat-seg").querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.value === "once"));
    document.getElementById("sf-days-seg").querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("is-active"));
    document.getElementById("sf-days-field").hidden = true;
    document.getElementById("sf-date-label").textContent = "Date";
    document.getElementById("sf-save").innerHTML = `${icon("check", "icon-sm")}Save schedule`;
  }

  function openForEdit(s) {
    editingId = s.id;
    const details = document.getElementById("sched-form-details");
    details.open = true;
    document.getElementById("sf-id").value = s.id;
    document.getElementById("sf-label").value = s.label || "";
    document.getElementById("sf-source").innerHTML = sourceOptionsHtml(s.source_id);
    document.getElementById("sf-date").value = s.start_date || "";
    document.getElementById("sf-start").value = s.start_time || "08:00";
    document.getElementById("sf-end").value = s.end_time || "14:00";
    document.getElementById("sf-overnight").checked = !!s.overnight;
    document.getElementById("sf-lead").value = s.join_lead_minutes ?? 5;
    document.getElementById("sf-keep-open").checked = !!s.keep_meeting_open;
    const repeatSeg = document.getElementById("sf-repeat-seg");
    repeatSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.value === s.repeat_mode));
    document.getElementById("sf-days-field").hidden = s.repeat_mode !== "custom";
    document.getElementById("sf-date-label").textContent = s.repeat_mode === "once" ? "Date" : "Starts on";
    const daysSeg = document.getElementById("sf-days-seg");
    daysSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", (s.custom_days || []).includes(Number(b.dataset.value))));
    document.getElementById("sf-save").innerHTML = `${icon("check", "icon-sm")}Save changes`;
    details.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // -------------------------------------------------------------- polling

  async function refresh() {
    try {
      const data = await apiFetch("/api/schedules");
      schedules = data.schedules || [];
      ntpSynced = data.ntp_synced;
      if (data.server_time) serverOffsetMs = new Date(data.server_time).getTime() - Date.now();
      lastFetchFailed = false;
    } catch (err) {
      lastFetchFailed = true;
    }
    renderList();
  }

  renderShell();
  refresh();
  setInterval(refresh, 5000);
  setInterval(renderList, 1000); // smooth per-second countdown between server polls
})();
