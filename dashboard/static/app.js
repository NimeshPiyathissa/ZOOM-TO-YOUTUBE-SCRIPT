// Shared helpers used by every page: CSRF-aware fetch, toasts, a custom
// confirm dialog (for destructive actions, with a focus trap), theme
// toggle, sidebar/drawer behavior, dropdown menus, and small rendering
// utilities (icons, badges, sparklines) used by the per-page scripts.

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : "";
}

async function apiFetch(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = Object.assign({}, opts.headers || {});
  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRF-Token"] = csrfToken();
  }
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(url, Object.assign({}, opts, { headers, credentials: "same-origin" }));
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    data = await res.json().catch(() => null);
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail)) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

// ---------------------------------------------------------------- icons

function icon(name, cls = "") {
  return `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}

// ---------------------------------------------------------------- toasts

function toast(message, type = "ok") {
  const wrap = document.getElementById("toasts");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.setAttribute("role", "status");
  const iconName = type === "err" ? "alert-circle" : "check-circle-2";
  el.innerHTML = `${icon(iconName)}<span>${message}</span>`;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);
  }, 5000);
}

// ---------------------------------------------------------------- focus trap

function trapFocus(container, onEscape) {
  const selector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  function focusables() { return Array.from(container.querySelectorAll(selector)).filter(el => el.offsetParent !== null); }
  const previouslyFocused = document.activeElement;
  const first = focusables()[0];
  if (first) first.focus();

  function onKeydown(e) {
    if (e.key === "Escape") { onEscape(); return; }
    if (e.key !== "Tab") return;
    const items = focusables();
    if (!items.length) return;
    const firstEl = items[0];
    const lastEl = items[items.length - 1];
    if (e.shiftKey && document.activeElement === firstEl) { e.preventDefault(); lastEl.focus(); }
    else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
  }
  container.addEventListener("keydown", onKeydown);
  return function release() {
    container.removeEventListener("keydown", onKeydown);
    if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
  };
}

// ---------------------------------------------------------------- confirm dialog

function confirmDialog(message, { danger = false, confirmText = "Confirm", requireText = null } = {}) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h3 id="confirm-title">${danger ? "Are you sure?" : "Confirm"}</h3>
        <p>${message}</p>
        ${requireText ? `<div class="field"><label class="label" for="confirm-input">Type "${requireText}" to confirm</label><input class="input" type="text" id="confirm-input"></div>` : ""}
        <div class="btn-row">
          <button id="confirm-cancel" class="btn btn-secondary">Cancel</button>
          <button id="confirm-ok" class="btn ${danger ? "btn-danger" : "btn-primary"}">${confirmText}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    let release;
    const cleanup = (result) => { release(); backdrop.remove(); resolve(result); };
    release = trapFocus(backdrop, () => cleanup(false));
    backdrop.querySelector("#confirm-cancel").onclick = () => cleanup(false);
    backdrop.querySelector("#confirm-ok").onclick = () => {
      if (requireText) {
        const val = backdrop.querySelector("#confirm-input").value;
        if (val !== requireText) { toast(`Type "${requireText}" exactly`, "err"); return; }
      }
      cleanup(true);
    };
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) cleanup(false); });
  });
}

// ---------------------------------------------------------------- theme

function initTheme() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  function paint(theme) {
    const isDark = theme ? theme === "dark" : !window.matchMedia("(prefers-color-scheme: light)").matches;
    btn.innerHTML = icon(isDark ? "moon" : "sun");
  }
  let stored = null;
  try { stored = localStorage.getItem("zsdash-theme"); } catch (e) {}
  if (stored) document.documentElement.setAttribute("data-theme", stored);
  paint(stored);
  btn.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("zsdash-theme", next); } catch (e) {}
    paint(next);
  });
}

// ---------------------------------------------------------------- sidebar / mobile drawer

function initSidebar() {
  const shell = document.querySelector(".app-shell");
  if (!shell) return;
  const sidebar = shell.querySelector(".sidebar");
  const collapseBtn = document.getElementById("sidebar-collapse");
  const drawerBtn = document.getElementById("sidebar-toggle");
  const backdrop = shell.querySelector(".sidebar-backdrop");
  const drawerMq = window.matchMedia("(max-width: 1023px)");

  // Below the drawer breakpoint the sidebar is off-canvas when closed; mark
  // it inert so its links aren't keyboard/screen-reader reachable while hidden.
  function syncInert() {
    if (sidebar) sidebar.inert = drawerMq.matches && !shell.classList.contains("is-drawer-open");
  }
  drawerMq.addEventListener("change", syncInert);
  syncInert();

  let collapsed = false;
  try { collapsed = localStorage.getItem("zsdash-sidebar-collapsed") === "1"; } catch (e) {}
  if (collapsed) shell.classList.add("is-collapsed");

  if (collapseBtn) collapseBtn.addEventListener("click", () => {
    shell.classList.toggle("is-collapsed");
    try { localStorage.setItem("zsdash-sidebar-collapsed", shell.classList.contains("is-collapsed") ? "1" : "0"); } catch (e) {}
  });

  function openDrawer() { shell.classList.add("is-drawer-open"); syncInert(); }
  function closeDrawer() { shell.classList.remove("is-drawer-open"); syncInert(); }
  if (drawerBtn) drawerBtn.addEventListener("click", () => {
    shell.classList.contains("is-drawer-open") ? closeDrawer() : openDrawer();
  });
  if (backdrop) backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
}

// ---------------------------------------------------------------- dropdown menus

function initMenus() {
  function setOpen(wrap, open) {
    wrap.classList.toggle("is-open", open);
    const menu = wrap.querySelector(".menu");
    if (menu) menu.inert = !open;
  }
  document.addEventListener("click", (e) => {
    const trigger = e.target.closest(".menu-trigger");
    document.querySelectorAll(".menu-wrap.is-open").forEach((w) => {
      if (!trigger || w !== trigger.closest(".menu-wrap")) setOpen(w, false);
    });
    if (trigger) {
      const wrap = trigger.closest(".menu-wrap");
      setOpen(wrap, !wrap.classList.contains("is-open"));
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".menu-wrap.is-open").forEach((w) => setOpen(w, false));
  });
}

// ---------------------------------------------------------------- segmented control

function initSegmented(container, onSelect) {
  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg-btn");
    if (!btn) return;
    container.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("is-active"));
    btn.classList.add("is-active");
    if (onSelect) onSelect(btn.dataset.value, btn);
  });
}

// ---------------------------------------------------------------- range <-> number sync

function syncRangeNumber(rangeEl, numberEl, outputEl) {
  const set = (v) => {
    rangeEl.value = v; numberEl.value = v;
    if (outputEl) outputEl.textContent = v;
  };
  rangeEl.addEventListener("input", () => set(rangeEl.value));
  numberEl.addEventListener("input", () => { if (numberEl.value !== "") set(numberEl.value); });
  set(numberEl.value || rangeEl.value);
}

// ---------------------------------------------------------------- sparklines

const _sparkHistory = new Map();

function pushSparkline(key, value, max = 40) {
  if (value == null || Number.isNaN(value)) return _sparkHistory.get(key) || [];
  const arr = _sparkHistory.get(key) || [];
  arr.push(value);
  if (arr.length > max) arr.shift();
  _sparkHistory.set(key, arr);
  return arr;
}

function renderSparkline(svgEl, values, { width = 100, height = 32, pad = 2 } = {}) {
  if (!svgEl || values.length < 2) return;
  svgEl.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svgEl.setAttribute("preserveAspectRatio", "none");
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const step = (width - pad * 2) / (values.length - 1);
  const points = values.map((v, i) => {
    const x = pad + i * step;
    const y = height - pad - ((v - min) / range) * (height - pad * 2);
    return [x, y];
  });
  const lineD = points.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const fillD = `${lineD} L${points[points.length - 1][0].toFixed(1)},${height} L${points[0][0].toFixed(1)},${height} Z`;
  let line = svgEl.querySelector("path.line");
  let fill = svgEl.querySelector("path.fill");
  if (!fill) { fill = document.createElementNS("http://www.w3.org/2000/svg", "path"); fill.setAttribute("class", "fill"); svgEl.appendChild(fill); }
  if (!line) { line = document.createElementNS("http://www.w3.org/2000/svg", "path"); line.setAttribute("class", "line"); svgEl.appendChild(line); }
  fill.setAttribute("d", fillD);
  line.setAttribute("d", lineD);
}

// per-core CPU bars: an attribute-driven SVG bar chart (no inline "style",
// so it works unchanged under the dashboard's strict style-src CSP)
function renderCpuCores(svgEl, values) {
  if (!svgEl || !values.length) return;
  const w = 100, h = 28, gap = 1.5;
  const bw = (w - gap * (values.length - 1)) / values.length;
  svgEl.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svgEl.setAttribute("preserveAspectRatio", "none");
  svgEl.innerHTML = values.map((p, i) => {
    const bh = Math.max(1, (Math.max(0, Math.min(100, p)) / 100) * h);
    const x = (i * (bw + gap)).toFixed(2);
    return `<rect class="cpu-bar-bg" x="${x}" y="0" width="${bw.toFixed(2)}" height="${h}" rx="1"></rect>` +
      `<rect class="cpu-bar-fill" x="${x}" y="${(h - bh).toFixed(2)}" width="${bw.toFixed(2)}" height="${bh.toFixed(2)}" rx="1"><title>${p.toFixed(0)}%</title></rect>`;
  }).join("");
}

// ---------------------------------------------------------------- misc formatting

function fmtUptime(seconds) {
  if (seconds == null) return "-";
  seconds = Math.floor(seconds);
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function badgeClass(activeState) {
  if (activeState === "active") return "badge-active";
  if (activeState === "failed") return "badge-failed";
  if (activeState === "activating" || activeState === "deactivating") return "badge-activating";
  return "badge-inactive";
}

function badgeHtml(activeState, text) {
  return `<span class="badge ${badgeClass(activeState)}"><span class="dot"></span>${text}</span>`;
}

// Single mapping from the backend's derived `phase` (STOPPED/STARTING/
// LIVE/RECONNECTING/FAILED - see control.py's _derive_phase) to a badge
// class, used everywhere a unit's status is shown so two places can
// never render it differently.
function phaseBadgeClass(phase) {
  if (phase === "LIVE") return "badge-active";
  if (phase === "FAILED") return "badge-failed";
  if (phase === "STARTING" || phase === "RECONNECTING") return "badge-activating";
  return "badge-inactive";
}

function fmtAgo(seconds) {
  if (seconds == null) return "";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

// ---------------------------------------------------------------- live region announcer

function announce(message) {
  let region = document.getElementById("sr-live-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "sr-live-region";
    region.className = "sr-only";
    region.setAttribute("aria-live", "polite");
    region.setAttribute("role", "status");
    document.body.appendChild(region);
  }
  region.textContent = message;
}

// ---------------------------------------------------------------- button loading helper

async function withLoading(btn, fn) {
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  btn.classList.add("is-loading");
  try { return await fn(); }
  finally { btn.disabled = wasDisabled; btn.classList.remove("is-loading"); }
}

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initSidebar();
  initMenus();
});
