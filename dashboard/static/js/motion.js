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
//  3. setProgress(el, pct) - drives a .progress-fill / .stat-progress-fill
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

  window.zsMotion = { reveal, setProgress };
  window.setProgress = setProgress;

  function init() {
    const main = document.getElementById("main");
    reveal(main);
    watchNumbers(document.body);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
