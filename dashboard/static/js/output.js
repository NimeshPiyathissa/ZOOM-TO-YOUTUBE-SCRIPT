// Output page (templates/output.html). Framing = /api/zoom/clean-broadcast (policy preserve=Auto, fit=Fit, fill=Fill;
// background blur/dark/black); quality = /api/output/preset. Nothing here starts, stops or restarts the stream; when the
// live encoder is behind, "Restart stream to apply" uses restartEncoder() (app.js: confirmation + one-time grant).
(function () {
  "use strict";
  const root = document.querySelector("[data-out-root]");
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const $$ = (s) => Array.from(root.querySelectorAll(s));
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const say = (m, k) => { if (typeof toast === "function") toast(m, k); };
  let last = null;
  let busy = false;

  function fact(k, v) { const el = root.querySelector(`[data-out-fact="${k}"]`); if (el) el.textContent = v; }

  function render(s, extraWarn) {
    last = s;
    const p = s.plan || null;
    const prefs = s.prefs || {};
    $$("[data-out-mode]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.outMode === (prefs.policy || "preserve"))));
    $$("[data-out-bg]").forEach((b) => b.classList.toggle("is-active", b.dataset.outBg === (prefs.background || "blur")));
    const st = s.settings || {};
    $$("[data-out-preset]").forEach((b) => {
      const pr = (s.output_presets || {})[b.dataset.outPreset] || {};
      const on = pr.RESOLUTION === st.RESOLUTION && String(pr.FPS) === String(st.FPS) && String(pr.VIDEO_BITRATE) === String(st.VIDEO_BITRATE) && pr.X264_PRESET === st.X264_PRESET;
      b.setAttribute("aria-checked", String(on));
    });
    const pill = root.querySelector("[data-out-state]");
    const state = !s.zoom_source ? "no-zoom" : !s.active ? "off" : (p && p.state) || "off";
    root.dataset.state = state;
    if (pill) pill.textContent = !s.zoom_source ? "Zoom source not active" : !s.active ? "Off (encoder uses the original framing)" : (p ? p.label : "—");
    $("[data-out-restore]").hidden = !(s.active || s.restore_available);
    $("[data-out-restart]").hidden = !(s.pending_restart && s.encoder_phase === "LIVE");
    $("[data-out-measure]").disabled = busy || !s.zoom_source;
    const enc = s.encoder || {};
    fact("encoder", `${String(s.encoder_phase || "—").toLowerCase()}${s.pending_restart ? " · restart needed to apply" : ""}`);
    fact("cpu", s.cpu_percent != null ? `${Math.round(s.cpu_percent)}%` : "—");
    fact("fps", enc.fps != null ? `${enc.fps} fps · ${enc.speed}x` : "not live");
    fact("drops", enc.fps != null ? `${enc.drop ?? 0} / ${enc.dup ?? 0}` : "—");
    fact("output", `${s.output || st.RESOLUTION || "—"} · ${st.FPS || "—"} fps · ${st.VIDEO_BITRATE || "—"} kbps · ${st.X264_PRESET || "—"}`);
    fact("display", s.display || "—");
    fact("content", p ? `${p.content_size} (${p.content_ratio}) at ${p.content.split(":").slice(0, 2).join(",")}` : "—");
    fact("scale", p ? (p.resampled ? `resampled ×${p.scale} (lanczos), once` : "1:1 pixels - no resampling") + ` · fill ${Math.max(...Object.values(p.bars))} px` : "—");
    $("[data-out-raw-size]").textContent = s.display || "";
    $("[data-out-final-size]").textContent = s.output || "";
    const items = [];
    if (extraWarn) items.push(extraWarn);
    if (s.pending_restart) items.push({ level: "warn", text: "The live stream still uses its previous framing/settings. Use “Restart stream to apply” (confirmation required) or they apply at the next Go Live." });
    ((p && p.warnings) || []).forEach((w) => items.push(w));
    $("[data-out-warnings]").innerHTML = items.map((w) => `<li class="cb-warn cb-warn-${esc(w.level)}">${w.html || esc(w.text)}</li>`).join("");
    const confirm = root.querySelector("[data-out-confirm-fill]");
    if (confirm) confirm.addEventListener("click", () => send("settings", { policy: "fill", confirm_crop: true }));
    drawOverlay(p);
  }

  function drawOverlay(p) {
    const ov = $("[data-out-overlay]");
    ov.innerHTML = "";
    if (!p || !p.layout || !p.layout.size) return;
    const [W, H] = p.layout.size.split("x").map(Number);
    const b = p.layout.bands || {};
    const box = (x, y, w, h, cls, label) => {
      if (w <= 0 || h <= 0) return;
      const d = document.createElement("div");
      d.className = cls;
      Object.assign(d.style, { left: `${(x / W) * 100}%`, top: `${(y / H) * 100}%`, width: `${(w / W) * 100}%`, height: `${(h / H) * 100}%` });
      if (label) d.dataset.label = label;
      ov.appendChild(d);
    };
    if (p.verified && p.policy !== "fit") {
      box(0, 0, W, b.top || 0, "cb-ui", "removed");
      box(0, H - (b.bottom || 0), W, b.bottom || 0, "cb-ui", "removed");
      box(0, b.top || 0, b.left || 0, H - (b.top || 0) - (b.bottom || 0), "cb-ui");
      box(W - (b.right || 0), b.top || 0, b.right || 0, H - (b.top || 0) - (b.bottom || 0), "cb-ui");
    }
    const [x, y, w, h] = String(p.broadcast_box || p.content).split(":").map(Number);
    box(x, y, w, h, "cb-keep", "to YouTube");
  }

  function loadPreviews() {
    const t = Date.now();
    const raw = $("[data-out-raw]"), fin = $("[data-out-final]");
    raw.onload = () => { raw.hidden = false; };
    fin.onload = () => { fin.hidden = false; };
    raw.src = `/api/zoom/clean-broadcast/preview.jpg?view=screen&t=${t}`;
    fin.src = `/api/zoom/clean-broadcast/preview.jpg?view=output&t=${t}`;
  }

  async function refresh() {
    if (busy) return;
    try { render(await apiFetch("/api/zoom/clean-broadcast")); } catch (err) { /* keep the last state */ }
  }

  function setBusy(on) {
    busy = on;
    root.setAttribute("aria-busy", String(on));
    $$("[data-out-mode], [data-out-bg], [data-out-preset], [data-out-measure], [data-out-restore]").forEach((el) => { el.disabled = on; });
  }

  async function send(action, extra = {}) {
    if (busy) return;
    setBusy(true);
    $("[data-out-status]").textContent = action === "apply" ? "Measuring the meeting on real frames…" : action === "restore" ? "Restoring…" : "Re-planning…";
    try {
      const prefs = last && last.prefs ? last.prefs : {};
      const body = Object.assign({ action }, action === "restore" ? {} : { policy: prefs.policy, background: prefs.background }, extra);
      const res = await apiFetch("/api/zoom/clean-broadcast", { method: "POST", body: JSON.stringify(body) });
      setBusy(false);
      render(await apiFetch("/api/zoom/clean-broadcast"));
      $("[data-out-status]").textContent = res.message || "";
      say(`${res.message || "Saved."} ${res.applies || ""}`.trim(), res.pending_restart ? "warn" : "ok");
      loadPreviews();
    } catch (err) {
      setBusy(false);
      $("[data-out-status]").textContent = "";
      if (err.status === 409) {
        render(last, { level: "danger", html: `${esc(err.message)} <button type="button" class="btn btn-danger btn-sm" data-out-confirm-fill>Cut and fill anyway</button>` });
        return;
      }
      say(err.message || "Failed", "err");
      render(last);
    }
  }

  async function choose(policy) {
    const prefs = Object.assign({}, (last && last.prefs) || {}, { policy });
    if (last) last.prefs = prefs;
    await send(last && last.active ? "settings" : "apply", { policy });
  }

  $$("[data-out-mode]").forEach((b) => b.addEventListener("click", () => choose(b.dataset.outMode)));
  $$("[data-out-bg]").forEach((b) => b.addEventListener("click", () => {
    if (last && last.prefs) last.prefs.background = b.dataset.outBg;
    send(last && last.active ? "settings" : "apply", { background: b.dataset.outBg });
  }));
  $$("[data-out-preset]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const r = await apiFetch("/api/output/preset", { method: "POST", body: JSON.stringify({ preset: b.dataset.outPreset }) });
      say(r.message, r.pending_restart ? "warn" : "ok");
      await refresh();
      loadPreviews();
    } catch (err) { say(err.message, "err"); }
  }));
  $("[data-out-measure]").addEventListener("click", () => send("apply"));
  $("[data-out-restore]").addEventListener("click", () => send("restore"));
  $("[data-out-refresh]").addEventListener("click", () => { refresh(); loadPreviews(); });
  $("[data-out-restart]").addEventListener("click", async () => { if (await restartEncoder()) setTimeout(refresh, 2500); });
  refresh().then(loadPreviews);
  setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
