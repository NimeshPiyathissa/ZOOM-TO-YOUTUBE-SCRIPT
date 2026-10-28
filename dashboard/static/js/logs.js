const UNITS = JSON.parse(document.getElementById("units-data").textContent);

let es = null;
let paused = false;
let autoScroll = true;
let lineNo = 0;
let level = "all";
const counts = { warn: 0, err: 0 };
const view = document.getElementById("log-view");
const search = document.getElementById("log-search");
const emptyEl = document.getElementById("log-empty");
const connEl = document.getElementById("log-conn");
const connText = document.getElementById("log-conn-text");

// Expected, non-error lines that the generic heuristic below would
// otherwise paint red. Mirrors app/logs.py's BENIGN_LINE_PATTERNS - keep
// the two in sync.
const BENIGN_LINE_PATTERNS = [
  /\[flv @ [^\]]+\] Failed to update header with correct (duration|filesize)/,
  /Exiting normally, received signal 15/,
];

function classify(line) {
  if (BENIGN_LINE_PATTERNS.some((re) => re.test(line))) return "";
  const l = line.toLowerCase();
  if (l.includes(" error") || l.includes("err") || l.includes("fail")) return "err";
  if (l.includes("warn")) return "warn";
  return "";
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function highlighted(text, query) {
  const escaped = escapeHtml(text);
  if (!query) return escaped;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return escaped;
  return escapeHtml(text.slice(0, idx)) + "<mark>" + escapeHtml(text.slice(idx, idx + query.length)) + "</mark>" + escapeHtml(text.slice(idx + query.length));
}

function setConn(state, text) {
  connEl.dataset.state = state;
  connText.textContent = text;
}

function paintCounts() {
  document.getElementById("log-count").textContent = lineNo;
  document.getElementById("log-count-warn").textContent = counts.warn;
  document.getElementById("log-count-err").textContent = counts.err;
}

// A line is visible when it matches both the text filter and the level
// filter ("warn" shows warnings and errors - anything at or above warn).
function lineVisible(el) {
  const q = search.value.toLowerCase();
  if (q && !el.dataset.raw.toLowerCase().includes(q)) return false;
  const lv = el.dataset.level;
  if (level === "err") return lv === "err";
  if (level === "warn") return lv === "err" || lv === "warn";
  return true;
}

function applyFilter() {
  const q = search.value;
  view.querySelectorAll(".log-line").forEach(el => {
    const show = lineVisible(el);
    el.hidden = !show;
    if (show) el.querySelector(".msg").innerHTML = highlighted(el.dataset.raw, q);
  });
}
search.addEventListener("input", applyFilter);

function connect(unit) {
  if (es) es.close();
  view.innerHTML = "";
  lineNo = 0; counts.warn = 0; counts.err = 0;
  paintCounts();
  emptyEl.hidden = false;
  document.getElementById("log-unit-label").textContent = unit;
  document.getElementById("log-download").href = `/api/logs/download?unit=${encodeURIComponent(unit)}`;
  document.querySelectorAll("#log-tabs .tab-btn").forEach(b => {
    const on = b.dataset.unit === unit;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-selected", String(on));
  });
  setConn("connecting", "Connecting");
  es = new EventSource(`/api/logs/stream?unit=${encodeURIComponent(unit)}`);
  es.onopen = () => setConn(paused ? "paused" : "live", paused ? "Paused" : "Streaming");
  es.onmessage = (ev) => {
    if (paused) return;
    const line = JSON.parse(ev.data);
    lineNo += 1;
    const cls = classify(line);
    if (cls) counts[cls] += 1;
    const div = document.createElement("div");
    div.className = "log-line " + cls;
    div.dataset.raw = line;
    div.dataset.level = cls || "info";
    div.innerHTML = `<span class="ln">${lineNo}</span><span class="msg">${highlighted(line, search.value)}</span>`;
    view.appendChild(div);
    emptyEl.hidden = true;
    if (view.children.length > 3000) view.removeChild(view.firstChild);
    if (!lineVisible(div)) div.hidden = true;
    if (autoScroll) view.scrollTop = view.scrollHeight;
    paintCounts();
  };
  es.onerror = () => { setConn("connecting", "Reconnecting"); /* browser auto-reconnects EventSource */ };
}

document.getElementById("log-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab-btn");
  if (btn) connect(btn.dataset.unit);
});
initSegmented(document.getElementById("log-level"), (value) => { level = value; applyFilter(); });
document.getElementById("log-pause").addEventListener("click", (e) => {
  paused = !paused;
  e.currentTarget.innerHTML = icon(paused ? "play" : "pause", "icon-sm") + (paused ? "Resume" : "Pause");
  setConn(paused ? "paused" : "live", paused ? "Paused" : "Streaming");
});
document.getElementById("log-autoscroll").addEventListener("change", (e) => { autoScroll = e.target.checked; });

connect(UNITS[0] || "");
