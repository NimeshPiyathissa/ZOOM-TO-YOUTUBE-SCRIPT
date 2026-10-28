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

const TOAST_ALIAS = { success: "ok", error: "err", danger: "err", warning: "warn" };

function toast(message, type = "ok") {
  const wrap = document.getElementById("toasts");
  if (!wrap) return;
  type = TOAST_ALIAS[type] || type;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.setAttribute("role", "status");
  const iconName = type === "err" ? "alert-circle" : type === "warn" ? "alert-triangle" : type === "info" ? "info" : "check-circle-2";
  el.innerHTML = `${icon(iconName)}<span>${message}</span>`;
  wrap.appendChild(el);
  const dismiss = () => {
    if (el.classList.contains("leaving")) return;
    el.classList.add("leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);
  };
  el.addEventListener("click", dismiss);
  setTimeout(dismiss, type === "err" ? 7000 : 5000);
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
        <div class="modal-icon icon-well ${danger ? "is-live" : ""}">${icon(danger ? "alert-triangle" : "info")}</div>
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
    const cleanup = (result) => {
      release();
      backdrop.classList.add("is-closing");
      const done = () => backdrop.remove();
      backdrop.addEventListener("animationend", done, { once: true });
      setTimeout(done, 250);
      resolve(result);
    };
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
//
// Light (white) / Dark (black), plus "System", which follows the OS. The
// server renders an explicit choice as <html data-theme> from the
// zsdash_theme cookie (no flash); "System" is a browser-local mode
// (localStorage zsdash-theme-mode) resolved by the inline bootstrap in
// base.html's <head> before first paint. The legacy "default" value the
// backend still accepts renders as Dark (tokens.css).
//
// Switching crossfades the whole page via the View Transitions API
// where available (a single GPU snapshot crossfade - nothing re-animates
// per element), and falls back to an instant swap elsewhere.

const THEME_ICON = { system: "monitor", white: "sun", black: "moon", default: "moon" };

function _themeMode() {
  if (document.documentElement.getAttribute("data-theme-mode") === "system") return "system";
  const t = document.documentElement.getAttribute("data-theme");
  return t === "default" ? "black" : (t || "system");
}

function _paintThemeSwitches(mode) {
  const resolved = document.documentElement.getAttribute("data-theme");
  document.querySelectorAll(".theme-switch-trigger").forEach((trigger) => {
    trigger.innerHTML = icon(mode === "system" ? THEME_ICON[resolved] || "monitor" : THEME_ICON[mode] || "sun");
  });
  document.querySelectorAll("[data-theme-choice]").forEach((item) => {
    const on = item.dataset.themeChoice === mode;
    item.classList.toggle("is-active", on);
    item.setAttribute("aria-checked", String(on));
  });
}

function _withThemeTransition(fn) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (document.startViewTransition && !reduce) {
    document.documentElement.classList.add("theme-transition");
    const vt = document.startViewTransition(fn);
    vt.finished.finally(() => document.documentElement.classList.remove("theme-transition"));
  } else {
    fn();
  }
}

async function _applyTheme(mode) {
  const root = document.documentElement;
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  _withThemeTransition(() => {
    if (mode === "system") {
      root.setAttribute("data-theme-mode", "system");
      root.setAttribute("data-theme", mq.matches ? "black" : "white");
    } else {
      root.removeAttribute("data-theme-mode");
      root.setAttribute("data-theme", mode);
    }
    _paintThemeSwitches(mode);
  });
  try { localStorage.setItem("zsdash-theme-mode", mode === "system" ? "system" : "explicit"); } catch (e) {}
  // The server only knows explicit themes; "System" keeps whatever it
  // last stored and the browser-local mode overrides it on every load.
  if (mode === "system") return;
  try { await apiFetch("/api/theme", { method: "POST", body: JSON.stringify({ theme: mode }) }); }
  catch (err) { toast("Could not save theme: " + err.message, "err"); }
}

function initTheme() {
  if (!document.querySelector("[data-theme-choice]")) return;
  _paintThemeSwitches(_themeMode());
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => _paintThemeSwitches(_themeMode()));
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-choice]");
    if (!btn) return;
    const wrap = btn.closest(".menu-wrap");
    if (wrap) { wrap.classList.remove("is-open"); const m = wrap.querySelector(".menu"); if (m) m.inert = true; }
    _applyTheme(btn.dataset.themeChoice);
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
  const drawerMq = window.matchMedia("(max-width: 767px)");

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
  if (collapseBtn) collapseBtn.setAttribute("aria-expanded", String(!collapsed));

  if (collapseBtn) collapseBtn.addEventListener("click", () => {
    const isCollapsed = shell.classList.toggle("is-collapsed");
    collapseBtn.setAttribute("aria-expanded", String(!isCollapsed));
    try { localStorage.setItem("zsdash-sidebar-collapsed", isCollapsed ? "1" : "0"); } catch (e) {}
  });

  // Below the drawer breakpoint the open sidebar is a modal off-canvas
  // panel (focus trap + scroll lock + role="dialog"); above it, the
  // exact same markup is just the persistent desktop rail, so none of
  // that modal-ness applies there - it's only ever added/removed here,
  // never left on at desktop widths.
  let releaseFocus = null;
  function openDrawer() {
    shell.classList.add("is-drawer-open");
    syncInert();
    if (drawerBtn) drawerBtn.setAttribute("aria-expanded", "true");
    if (drawerMq.matches && sidebar) {
      sidebar.setAttribute("role", "dialog");
      sidebar.setAttribute("aria-modal", "true");
      sidebar.setAttribute("aria-label", "Navigation");
      document.body.style.overflow = "hidden";
      releaseFocus = trapFocus(sidebar, closeDrawer);
    }
  }
  function closeDrawer() {
    shell.classList.remove("is-drawer-open");
    syncInert();
    if (drawerBtn) drawerBtn.setAttribute("aria-expanded", "false");
    if (sidebar) { sidebar.removeAttribute("role"); sidebar.removeAttribute("aria-modal"); sidebar.removeAttribute("aria-label"); }
    document.body.style.overflow = "";
    if (releaseFocus) { releaseFocus(); releaseFocus = null; }
  }
  if (drawerBtn) drawerBtn.addEventListener("click", () => {
    shell.classList.contains("is-drawer-open") ? closeDrawer() : openDrawer();
  });
  if (backdrop) backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && shell.classList.contains("is-drawer-open")) closeDrawer(); });

  // Swipe-to-dismiss: tracks the finger 1:1 via a temporary inline
  // transform (cleared on release so the CSS transition/class takes
  // back over), no rAF loop needed since touchmove already paces this.
  if (sidebar) {
    let startX = null, currentX = 0, dragging = false, startTime = 0;
    sidebar.addEventListener("touchstart", (e) => {
      if (!shell.classList.contains("is-drawer-open") || !drawerMq.matches) return;
      startX = e.touches[0].clientX; currentX = startX; startTime = Date.now(); dragging = true;
      sidebar.style.transition = "none";
    }, { passive: true });
    sidebar.addEventListener("touchmove", (e) => {
      if (!dragging) return;
      currentX = e.touches[0].clientX;
      sidebar.style.transform = `translateX(${Math.min(0, currentX - startX)}px)`;
    }, { passive: true });
    function endDrag() {
      if (!dragging) return;
      dragging = false;
      const dx = currentX - startX;
      const velocity = dx / Math.max(Date.now() - startTime, 1);
      sidebar.style.transition = "";
      sidebar.style.transform = "";
      if (dx < -60 || velocity < -0.5) closeDrawer();
      startX = null;
    }
    sidebar.addEventListener("touchend", endDrag);
    sidebar.addEventListener("touchcancel", endDrag);
  }
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
  if (!container) return;
  container.querySelectorAll(".seg-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.classList.contains("is-active"))));
  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg-btn");
    if (!btn) return;
    container.querySelectorAll(".seg-btn").forEach((b) => { b.classList.remove("is-active"); b.setAttribute("aria-pressed", "false"); });
    btn.classList.add("is-active");
    btn.setAttribute("aria-pressed", "true");
    if (onSelect) onSelect(btn.dataset.value, btn);
  });
}

// ---------------------------------------------------------------- range <-> number sync

function syncRangeNumber(rangeEl, numberEl, outputEl) {
  if (!rangeEl || !numberEl) return;
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

// Sparklines morph between updates: when the point count is unchanged
// (the steady state once the history window is full) the previous and
// next y-values are interpolated over a short rAF tween, so the line
// glides rather than jumps. Reduced-motion users get the instant swap.
const _sparkPrev = new WeakMap();

function _sparkPoints(values, width, height, pad) {
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const step = (width - pad * 2) / (values.length - 1);
  return values.map((v, i) => [pad + i * step, height - pad - ((v - min) / range) * (height - pad * 2)]);
}

function _sparkPaint(svgEl, points, height) {
  const lineD = points.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const fillD = `${lineD} L${points[points.length - 1][0].toFixed(1)},${height} L${points[0][0].toFixed(1)},${height} Z`;
  let line = svgEl.querySelector("path.line");
  let fill = svgEl.querySelector("path.fill");
  if (!fill) { fill = document.createElementNS("http://www.w3.org/2000/svg", "path"); fill.setAttribute("class", "fill"); svgEl.appendChild(fill); }
  if (!line) { line = document.createElementNS("http://www.w3.org/2000/svg", "path"); line.setAttribute("class", "line"); svgEl.appendChild(line); }
  fill.setAttribute("d", fillD);
  line.setAttribute("d", lineD);
}

function renderSparkline(svgEl, values, { width = 100, height = 32, pad = 2 } = {}) {
  if (!svgEl || values.length < 2) return;
  svgEl.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svgEl.setAttribute("preserveAspectRatio", "none");
  const next = _sparkPoints(values, width, height, pad);
  const prevState = _sparkPrev.get(svgEl);
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (prevState && prevState.raf) cancelAnimationFrame(prevState.raf);
  const from = prevState && prevState.points;
  if (reduce || !from || from.length !== next.length || document.hidden) {
    _sparkPaint(svgEl, next, height);
    _sparkPrev.set(svgEl, { points: next });
    return;
  }
  const state = { points: next, raf: 0 };
  _sparkPrev.set(svgEl, state);
  const t0 = performance.now(), dur = 420;
  const tick = (now) => {
    const t = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - t, 3);
    _sparkPaint(svgEl, next.map((p, i) => [p[0], from[i][1] + (p[1] - from[i][1]) * e]), height);
    if (t < 1) state.raf = requestAnimationFrame(tick);
  };
  state.raf = requestAnimationFrame(tick);
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
