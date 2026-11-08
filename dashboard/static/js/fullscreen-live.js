// "Fullscreen Live" control - shared by the Remote GUI (/vnc) and Live Studio pages (templates/_fullscreen_live.html).
// POST /api/zoom/fullscreen-live {action: apply|restore} frames the Zoom meeting for the broadcast (see
// app/fullscreen_live.py). It never starts, stops or restarts the stream: when the encoder is live the new framing is
// saved and reported as pending for the next authorized start. States on [data-fsl-root]: idle, loading, success,
// limited, pending, error. Other page code can listen for the "fsl:state" window event (vnc.js draws the broadcast
// frame over the remote screen from it).
(function () {
  "use strict";
  const roots = Array.from(document.querySelectorAll("[data-fsl-root]"));
  if (!roots.length) return;
  let last = null;
  let busy = false;

  const say = (msg, kind) => { if (typeof toast === "function") toast(msg, kind); };

  function stateOf(s) {
    if (!s || !s.active) return "idle";
    if (s.pending_restart) return "pending";
    const st = (s.plan && s.plan.state) || "";
    return st === "limited" ? "limited" : "success";
  }

  function statusText(s) {
    if (!s) return "";
    if (s.zoom_source === false) return "Zoom source not active";
    if (!s.active) return "";
    const p = s.plan || {};
    const head = p.state === "native" ? "Native 16:9 · no borders"
      : p.state === "optimized" ? `16:9 · black border removed (×${p.scale})`
      : `Whole picture kept · ${p.content_ratio || "source"} content`;
    return s.pending_restart ? `${head} · applies at next authorized start` : head;
  }

  function detail(s) {
    const p = (s && s.plan) || {};
    const parts = [p.summary || ""];
    (p.limitations || []).forEach((l) => parts.push("Limitation: " + l));
    (p.issues || []).forEach((i) => parts.push(`${i.detail} ${i.fix}`));
    if (s && s.applies) parts.push(s.applies);
    return parts.filter(Boolean).join("\n");
  }

  function render(s, opts = {}) {
    last = s;
    const state = opts.state || stateOf(s);
    roots.forEach((root) => {
      root.dataset.state = state;
      const apply = root.querySelector("[data-fsl-apply]");
      const restore = root.querySelector("[data-fsl-restore]");
      const status = root.querySelector("[data-fsl-status]");
      if (apply) {
        apply.disabled = state === "loading" || (s && s.zoom_source === false);
        apply.setAttribute("aria-busy", String(state === "loading"));
        apply.setAttribute("aria-pressed", String(!!(s && s.active)));
        const label = apply.querySelector(".fsl-label");
        if (label) label.textContent = state === "loading" ? "Framing…" : (s && s.active ? "Re-check Fullscreen" : "Fullscreen Live");
      }
      if (restore) {
        restore.hidden = !(s && (s.active || s.restore_available));
        restore.disabled = state === "loading";
      }
      if (status) {
        status.textContent = opts.text != null ? opts.text : statusText(s);
        status.title = opts.text != null ? opts.text : detail(s);
      }
    });
    window.dispatchEvent(new CustomEvent("fsl:state", { detail: s || {} }));
  }

  async function refresh() {
    if (busy) return;
    try { render(await apiFetch("/api/zoom/fullscreen-live")); } catch (err) { /* keep the last state */ }
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    render(last, { state: "loading", text: action === "apply" ? "Fullscreen window, measuring the meeting…" : "Restoring…" });
    try {
      const res = await apiFetch("/api/zoom/fullscreen-live", { method: "POST", body: JSON.stringify({ action }) });
      busy = false;
      render(res);
      if (action === "restore") {
        say(res.message || "Original framing restored.", res.pending_restart ? "warn" : "ok");
      } else {
        const kind = res.state === "limited" || res.pending_restart ? "warn" : "ok";
        const lim = (res.plan && res.plan.limitations && res.plan.limitations[0]) ? " " + res.plan.limitations[0] : "";
        say(`${res.message}${res.state === "limited" ? "" : lim} ${res.applies || ""}`.trim(), kind);
      }
    } catch (err) {
      busy = false;
      render(last, { state: "error", text: err.message || "Fullscreen Live failed" });
      say(err.message || "Fullscreen Live failed", "err");
    }
  }

  roots.forEach((root) => {
    root.querySelector("[data-fsl-apply]")?.addEventListener("click", () => run("apply"));
    root.querySelector("[data-fsl-restore]")?.addEventListener("click", () => run("restore"));
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
})();
