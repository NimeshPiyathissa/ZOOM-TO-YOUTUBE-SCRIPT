const UNITS = JSON.parse(document.getElementById("units-data").textContent);

let es = null;
let paused = false;
let autoScroll = true;
let lineNo = 0;
const view = document.getElementById("log-view");
const search = document.getElementById("log-search");

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

function applyFilter() {
  const q = search.value;
  view.querySelectorAll(".log-line").forEach(el => {
    const raw = el.dataset.raw;
    const hide = q && !raw.toLowerCase().includes(q.toLowerCase());
    el.hidden = hide;
    if (!hide) el.querySelector(".msg").innerHTML = highlighted(raw, q);
  });
}
search.addEventListener("input", applyFilter);

function connect(unit) {
  if (es) es.close();
  view.innerHTML = "";
  lineNo = 0;
  document.getElementById("log-download").href = `/api/logs/download?unit=${encodeURIComponent(unit)}`;
  document.querySelectorAll("#log-tabs .tab-btn").forEach(b => b.classList.toggle("is-active", b.dataset.unit === unit));
  es = new EventSource(`/api/logs/stream?unit=${encodeURIComponent(unit)}`);
  es.onmessage = (ev) => {
    if (paused) return;
    const line = JSON.parse(ev.data);
    lineNo += 1;
    const div = document.createElement("div");
    div.className = "log-line " + classify(line);
    div.dataset.raw = line;
    div.innerHTML = `<span class="ln">${lineNo}</span><span class="msg">${highlighted(line, search.value)}</span>`;
    view.appendChild(div);
    if (view.children.length > 3000) view.removeChild(view.firstChild);
    if (search.value && !line.toLowerCase().includes(search.value.toLowerCase())) div.hidden = true;
    if (autoScroll) view.scrollTop = view.scrollHeight;
  };
  es.onerror = () => { /* browser auto-reconnects EventSource */ };
}

document.getElementById("log-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab-btn");
  if (btn) connect(btn.dataset.unit);
});
document.getElementById("log-pause").addEventListener("click", (e) => {
  paused = !paused;
  e.currentTarget.innerHTML = icon(paused ? "play" : "pause", "icon-sm") + (paused ? "Resume" : "Pause");
});
document.getElementById("log-autoscroll").addEventListener("change", (e) => { autoScroll = e.target.checked; });

connect(UNITS[0] || "");
