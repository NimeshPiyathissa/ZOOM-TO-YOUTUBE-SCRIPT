// "Clean Broadcast" control - Remote GUI quick bar (compact) and Live Studio panel (templates/_clean_broadcast.html).
// POST /api/zoom/clean-broadcast {action: apply|settings|restore} measures Zoom's own UI on real frames and frames only
// the meeting picture (app/clean_broadcast.py). It never starts, stops or restarts the stream: while live the new framing
// is saved and reported as pending for the next authorized start. States on [data-cb-root]: idle, loading, clean,
// composed, warn (unverified/cropped/pending), error. The "fsl:state" window event tells vnc.js which part of the screen
// the broadcast shows, so the Remote GUI outlines it.
(function () {
  "use strict";
  const roots = Array.from(document.querySelectorAll("[data-cb-root]"));
  if (!roots.length) return;
  let last = null;
  let busy = false;
  const say = (msg, kind) => { if (typeof toast === "function") toast(msg, kind); };
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const SOURCE_LABEL = { unknown: "Not checked", "360p": "640×360", "540p": "960×540", "720p": "1280×720", "1080p": "1920×1080" };

  function stateOf(s) {
    if (!s || !s.active || !s.plan) return "idle";
    if (s.pending_restart) return "warn";
    const st = s.plan.state;
    return st === "clean" ? "clean" : st === "composed" ? "composed" : "warn";
  }

  function statusText(s) {
    if (!s) return "";
    if (s.zoom_source === false) return "Zoom source not active";
    if (!s.active || !s.plan) return "";
    const head = s.plan.label || s.plan.state;
    return s.pending_restart ? `${head} · applies at next authorized start` : head;
  }

  function sideList(removed) {
    const parts = Object.entries(removed || {}).filter(([, v]) => v && v.px > 0).map(([k, v]) => `${k} ${v.px} px${v.kind === "black" ? " (black)" : ""}`);
    return parts.length ? parts.join(", ") : "none";
  }

  function renderPanel(root, s) {
    const p = (s && s.plan) || null;
    const prefs = (s && s.prefs) || {};
    root.querySelectorAll("[data-cb-policy]").forEach((r) => { r.checked = r.value === (prefs.policy || "preserve"); });
    const bg = root.querySelector("[data-cb-background]");
    if (bg && prefs.background) bg.value = prefs.background;
    const src = root.querySelector("[data-cb-source]");
    if (src && prefs.source) src.value = prefs.source;
    const fact = (k, v) => { const el = root.querySelector(`[data-cb-fact="${k}"]`); if (el) el.textContent = v; };
    fact("source", SOURCE_LABEL[prefs.source || "unknown"] + (p && p.magnify ? ` · shown ×${p.magnify}` : ""));
    fact("content", p ? `${p.content_size} at ${p.content.split(":").slice(0, 2).join(",")}` : "—");
    fact("capture", p ? p.capture_size : (s && s.display) || "—");
    fact("output", p ? p.output : (s && s.output) || "—");
    fact("ratio", p ? p.content_ratio : "—");
    fact("scale", p ? `×${p.scale} · bars ${Math.max(...Object.values(p.bars))} px` : "—");
    fact("removed", p ? (p.verified ? sideList(p.removed) : "not verified") : "—");
    fact("cpu", s ? `${s.cpu_percent != null ? Math.round(s.cpu_percent) + "%" : "—"} · ${String(s.encoder_phase || "—").toLowerCase()}` : "—");
    const size = root.querySelector("[data-cb-output-size]");
    if (size) size.textContent = p ? p.output : "";
    const list = root.querySelector("[data-cb-warnings]");
    if (list) {
      const items = [];
      if (s && s.pending_restart) items.push({ level: "warn", text: "The live stream still uses its old framing; this applies at the next authorized start or restart (never automatic)." });
      if (s && s.kept) items.push({ level: "warn", text: s.message });
      ((p && p.warnings) || []).forEach((w) => items.push(w));
      list.innerHTML = items.map((w) => `<li class="cb-warn cb-warn-${esc(w.level)}">${esc(w.text)}</li>`).join("");
    }
    drawOverlay(root, p);
  }

  function drawOverlay(root, p) {
    const ov = root.querySelector("[data-cb-overlay]");
    if (!ov) return;
    ov.innerHTML = "";
    if (!p || !p.layout || !p.layout.size) return;
    const [W, H] = p.layout.size.split("x").map(Number);
    const b = (p.layout.bands) || {};
    const box = (x, y, w, h, cls, label) => {
      if (w <= 0 || h <= 0) return;
      const d = document.createElement("div");
      d.className = cls;
      Object.assign(d.style, { left: `${(x / W) * 100}%`, top: `${(y / H) * 100}%`, width: `${(w / W) * 100}%`, height: `${(h / H) * 100}%` });
      if (label) d.dataset.label = label;
      ov.appendChild(d);
    };
    if (p.verified) {
      box(0, 0, W, b.top || 0, "cb-ui", "Zoom UI");
      box(0, H - (b.bottom || 0), W, b.bottom || 0, "cb-ui", "Zoom UI");
      box(0, b.top || 0, b.left || 0, H - (b.top || 0) - (b.bottom || 0), "cb-ui");
      box(W - (b.right || 0), b.top || 0, b.right || 0, H - (b.top || 0) - (b.bottom || 0), "cb-ui", (b.right || 0) > 40 ? "Zoom panel" : "");
    }
    const [x, y, w, h] = String(p.broadcast_box || p.content).split(":").map(Number);
    box(x, y, w, h, "cb-keep", "Broadcast");
  }

  function render(s, opts = {}) {
    last = s;
    const state = opts.state || stateOf(s);
    roots.forEach((root) => {
      root.dataset.state = state;
      const apply = root.querySelector("[data-cb-apply]");
      const restore = root.querySelector("[data-cb-restore]");
      const status = root.querySelector("[data-cb-status]");
      if (apply) {
        apply.disabled = state === "loading" || (s && s.zoom_source === false);
        apply.setAttribute("aria-busy", String(state === "loading"));
        apply.setAttribute("aria-pressed", String(!!(s && s.active)));
        const label = apply.querySelector(".cb-label");
        if (label) label.textContent = state === "loading" ? "Measuring…" : (s && s.active ? "Re-measure" : "Clean Broadcast");
      }
      if (restore) {
        restore.hidden = !(s && (s.active || s.restore_available));
        restore.disabled = state === "loading";
      }
      if (status) {
        status.textContent = opts.text != null ? opts.text : statusText(s);
        status.title = opts.text != null ? opts.text : ((s && s.plan && s.plan.summary) || "");
      }
      if (root.dataset.variant === "panel" && s && opts.text == null) renderPanel(root, s);
    });
    const box = s && s.active && s.plan ? (s.plan.broadcast_box || s.plan.content) : "";
    window.dispatchEvent(new CustomEvent("fsl:state", { detail: { active: !!box, box } }));
  }

  function loadPreviews(extra = "") {
    const t = Date.now();
    roots.forEach((root) => {
      const scr = root.querySelector("[data-cb-screen]");
      const out = root.querySelector("[data-cb-output]");
      if (scr) { scr.onload = () => { scr.hidden = false; }; scr.src = `/api/zoom/clean-broadcast/preview.jpg?view=screen&t=${t}`; }
      if (out) { out.onload = () => { out.hidden = false; }; out.src = `/api/zoom/clean-broadcast/preview.jpg?view=output&t=${t}${extra}`; }
    });
  }

  async function refresh() {
    if (busy) return;
    try { render(await apiFetch("/api/zoom/clean-broadcast")); } catch (err) { /* keep the last state */ }
  }

  function prefsFrom(root) {
    const pol = root && root.querySelector("[data-cb-policy]:checked");
    const bg = root && root.querySelector("[data-cb-background]");
    const src = root && root.querySelector("[data-cb-source]");
    return { policy: pol ? pol.value : undefined, background: bg ? bg.value : undefined, source: src ? src.value : undefined };
  }

  async function run(action, root, extra = {}) {
    if (busy) return;
    busy = true;
    render(last, { state: "loading", text: action === "restore" ? "Restoring…" : action === "apply" ? "Fullscreen window, measuring Zoom's UI on real frames…" : "Re-planning…" });
    try {
      const body = Object.assign({ action }, action === "restore" ? {} : prefsFrom(root), extra);
      const res = await apiFetch("/api/zoom/clean-broadcast", { method: "POST", body: JSON.stringify(body) });
      busy = false;
      const full = await apiFetch("/api/zoom/clean-broadcast").catch(() => res);
      render(Object.assign({}, full, { kept: res.kept, message: res.message }));
      const st = res.state || "";
      say(`${res.message || "Done."} ${res.applies || ""}`.trim(), st === "clean" || st === "composed" || action === "restore" ? (res.pending_restart ? "warn" : "ok") : "warn");
      loadPreviews();
    } catch (err) {
      busy = false;
      if (err.status === 409 && root) {
        render(last);
        const list = root.querySelector("[data-cb-warnings]");
        if (list) {
          const li = document.createElement("li");
          li.className = "cb-warn cb-warn-danger";
          li.innerHTML = `${esc(err.message)} <button type="button" class="btn btn-danger btn-sm" data-cb-confirm-fill>Cut and fill anyway</button>`;
          list.prepend(li);
          li.querySelector("[data-cb-confirm-fill]").addEventListener("click", () => run(action, root, { policy: "fill", confirm_crop: true }));
        }
        root.querySelectorAll("[data-cb-policy]").forEach((r) => { r.checked = r.value === ((last && last.prefs && last.prefs.policy) || "preserve"); });
        return;
      }
      render(last, { state: "error", text: err.message || "Clean Broadcast failed" });
      say(err.message || "Clean Broadcast failed", "err");
    }
  }

  roots.forEach((root) => {
    root.querySelector("[data-cb-apply]")?.addEventListener("click", () => run("apply", root));
    root.querySelector("[data-cb-restore]")?.addEventListener("click", () => run("restore", root));
    root.querySelector("[data-cb-preview]")?.addEventListener("click", () => loadPreviews());
    root.querySelectorAll("[data-cb-policy], [data-cb-background], [data-cb-source]").forEach((el) =>
      el.addEventListener("change", () => run("settings", root)));
  });
  refresh().then(() => { if (roots.some((r) => r.dataset.variant === "panel")) loadPreviews(); });
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
})();
