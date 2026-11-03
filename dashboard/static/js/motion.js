// Motion layer shared by every page. Dependency-free, transform/opacity
// only, and fully inert under prefers-reduced-motion.
//
//  1. Staggered entrance - cards visible on first paint rise in one
//     beat apart (CSS .reveal + --i, see base.css). The class is removed
//     after the animation so hover lifts aren't pinned by fill-mode.
//  2. Count-up - any live number inside .stat-value / .count-chip /
//     .hero-chip / [data-countup] tweens from its previous value when a
//     page script rewrites its text. Page scripts don't need to know: a
//     MutationObserver notices the new text and animates toward it,
//     always landing on the exact string the page wrote.
//  3. Sidebar indicator - one glowing bar that glides to the active nav
//     item (transform only), starting from where it sat on the previous
//     page (sessionStorage), so navigation feels continuous even though
//     every page is a full load.
//  4. Card spotlight - the hovered card's highlight follows the pointer
//     via two custom properties (rAF-throttled, only the hovered card).
//  5. setProgress(el, pct) - drives a .progress-fill / .stat-progress-fill
//     with transform: scaleX (never width) and warn/crit tinting.
(function () {
  const reduceMq = window.matchMedia("(prefers-reduced-motion: reduce)");
  const reduced = () => reduceMq.matches;

  // ------------------------------------------------------------ 1. stagger
  const REVEAL_SEL = [
    ".page-header", ".card", ".card-3d", ".stat-tile-glass", ".bento-tile",
    ".banner:not([hidden])", "[data-reveal]",
  ].join(",");
  const MAX_STAGGER = 14;

  function reveal(root) {
    if (reduced() || !root) return;
    const limit = window.innerHeight * 1.4;
    let i = 0;
    root.querySelectorAll(REVEAL_SEL).forEach((el) => {
      if (el.closest(".reveal") || el.closest("[data-no-reveal]") || el.offsetParent === null) return;
      const top = el.getBoundingClientRect().top;
      if (top > limit) return;
      el.style.setProperty("--i", String(Math.min(i++, MAX_STAGGER)));
      el.classList.add("reveal");
      const done = (e) => {
        if (e.target !== el) return;
        el.classList.remove("reveal");
        el.style.removeProperty("--i");
        el.removeEventListener("animationend", done);
      };
      el.addEventListener("animationend", done);
    });
  }

  // ------------------------------------------------------------ 2. count-up
  const COUNT_SEL = ".stat-value, .count-chip, .hero-chip, [data-countup]";
  const NUM_RE = /-?\d[\d,]*(?:\.\d+)?/;
  const lastValue = new WeakMap();   // element -> last numeric value seen
  const written = new WeakMap();     // text node -> string we wrote mid-tween
  const running = new WeakMap();     // text node -> rAF id

  function numericNode(el) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) { if (/\d/.test(n.data)) return n; }
    return null;
  }

  function fmt(v, decimals, commas) {
    const s = v.toFixed(decimals);
    if (!commas) return s;
    const [a, b] = s.split(".");
    return a.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (b ? "." + b : "");
  }

  function tween(node, finalText, match, from, to) {
    const prev = running.get(node);
    if (prev) cancelAnimationFrame(prev);
    const decimals = (match[0].split(".")[1] || "").length;
    const commas = match[0].includes(",");
    const prefix = finalText.slice(0, match.index);
    const suffix = finalText.slice(match.index + match[0].length);
    const dur = Math.min(700, 320 + Math.abs(to - from) * 2);
    const t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - t, 3);
      const str = t >= 1 ? finalText : prefix + fmt(from + (to - from) * e, decimals, commas) + suffix;
      written.set(node, str);
      node.data = str;
      if (t < 1) running.set(node, requestAnimationFrame(step));
      else running.delete(node);
    };
    running.set(node, requestAnimationFrame(step));
  }

  function onTextChange(el) {
    const node = numericNode(el);
    if (!node) { lastValue.delete(el); return; }
    const text = node.data;
    if (written.get(node) === text) return;          // our own tween frame
    const m = text.match(NUM_RE);
    if (!m) return;
    const to = parseFloat(m[0].replace(/,/g, ""));
    if (!Number.isFinite(to)) return;
    const from = lastValue.has(el) ? lastValue.get(el) : (el.matches(".stat-value") ? 0 : null);
    lastValue.set(el, to);
    if (from == null || from === to || reduced() || document.hidden) return;
    tween(node, text, m, from, to);
  }

  function watchNumbers(root) {
    if (!root) return;
    root.querySelectorAll(COUNT_SEL).forEach((el) => {
      const n = numericNode(el);
      if (n) { const m = n.data.match(NUM_RE); if (m) lastValue.set(el, parseFloat(m[0].replace(/,/g, ""))); }
    });
    const mo = new MutationObserver((muts) => {
      const touched = new Set();
      for (const mu of muts) {
        const target = mu.target.nodeType === 3 ? mu.target.parentElement : mu.target;
        const el = target && target.closest && target.closest(COUNT_SEL);
        if (el) touched.add(el);
      }
      touched.forEach(onTextChange);
    });
    mo.observe(root, { subtree: true, childList: true, characterData: true });
  }

  // ------------------------------------------------------------ 3. progress
  function setProgress(el, pct, { warn = 75, crit = 90 } = {}) {
    if (!el) return;
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.transform = `scaleX(${(p / 100).toFixed(4)})`;
    el.classList.toggle("is-warn", p >= warn && p < crit);
    el.classList.toggle("is-crit", p >= crit);
  }

  // ------------------------------------------------------------ 3. nav indicator
  function mountNavIndicator() {
    const nav = document.querySelector(".sidebar-nav");
    if (!nav) return;
    let bar = nav.querySelector(".nav-indicator");
    if (!bar) { bar = document.createElement("span"); bar.className = "nav-indicator"; bar.setAttribute("aria-hidden", "true"); nav.appendChild(bar); }
    const place = (animate) => {
      const active = nav.querySelector(".nav-item.active");
      if (!active) { nav.classList.remove("has-indicator"); return; }
      const top = active.offsetTop + (active.offsetHeight - bar.offsetHeight) / 2;
      if (!animate) bar.style.transition = "none";
      bar.style.setProperty("--ind-y", top + "px");
      nav.classList.add("has-indicator");
      if (!animate) { void bar.offsetWidth; bar.style.transition = ""; }
    };
    let prev = null;
    try { prev = sessionStorage.getItem("zs-nav-y"); } catch (e) { /* private mode */ }
    const active = nav.querySelector(".nav-item.active");
    if (prev != null && active && !reduced()) {
      bar.style.transition = "none";
      bar.style.setProperty("--ind-y", prev + "px");
      nav.classList.add("has-indicator");
      void bar.offsetWidth;
      bar.style.transition = "";
      requestAnimationFrame(() => place(true));
    } else place(false);
    if (active) { try { sessionStorage.setItem("zs-nav-y", String(active.offsetTop + (active.offsetHeight - bar.offsetHeight) / 2)); } catch (e) { /* ignore */ } }
    window.addEventListener("resize", () => place(false));
    const shell = document.querySelector(".app-shell");
    if (shell) new MutationObserver(() => requestAnimationFrame(() => place(false))).observe(shell, { attributes: true, attributeFilter: ["class"] });
  }

  // ------------------------------------------------------------ 4. card spotlight
  function mountSpotlight() {
    if (reduced() || !window.matchMedia("(hover: hover)").matches) return;
    let raf = 0, el = null, x = 0, y = 0;
    document.addEventListener("pointermove", (e) => {
      const card = e.target.closest && e.target.closest(".card, .card-3d");
      if (!card) return;
      el = card; x = e.clientX; y = e.clientY;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const r = el.getBoundingClientRect();
        el.style.setProperty("--mx", (x - r.left) + "px");
        el.style.setProperty("--my", (y - r.top) + "px");
      });
    }, { passive: true });
  }

  window.zsMotion = { reveal, setProgress };
  window.setProgress = setProgress;

  function init() {
    const main = document.getElementById("main");
    reveal(main);
    watchNumbers(document.body);
    mountNavIndicator();
    mountSpotlight();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
