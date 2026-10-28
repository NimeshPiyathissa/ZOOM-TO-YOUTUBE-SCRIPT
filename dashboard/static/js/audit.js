const ROWS = JSON.parse(document.getElementById("audit-rows-data").textContent);

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

function fmtRel(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

// Category drives the action badge color and the category filter. Pure
// presentation - derived from the action name the backend already logs.
function kindOf(action) {
  const a = (action || "").toLowerCase();
  if (/fail|locked|denied|error|invalid/.test(a)) return "danger";
  if (/login|logout|session|password/.test(a)) return "auth";
  if (/start|stop|restart|go_live|go-live|stream|unit|record|reboot/.test(a)) return "service";
  return "change";
}
const KIND_BADGE = { danger: "badge-failed", auth: "badge-active", service: "badge-accent", change: "badge-info" };

const body = document.getElementById("audit-body");
const search = document.getElementById("audit-search");
const userSel = document.getElementById("audit-user-filter");
const kindSel = document.getElementById("audit-kind-filter");

function rowHtml(r) {
  const kind = kindOf(r.action);
  const user = r.username || "-";
  return `
    <tr>
      <td class="when"><span class="rel">${esc(fmtRel(r.ts))}</span><span class="abs tnum">${esc(fmtTs(r.ts))}</span></td>
      <td><span class="audit-user"><span class="av" aria-hidden="true">${esc(user.slice(0, 1))}</span>${esc(user)}</span></td>
      <td><span class="badge action-badge ${KIND_BADGE[kind]}"><span class="dot"></span>${esc(r.action)}</span></td>
      <td class="detail">${esc(r.detail || "-")}</td>
      <td class="ip">${esc(r.ip || "-")}</td>
    </tr>`;
}

function render() {
  const q = (search.value || "").toLowerCase();
  const u = userSel.value;
  const k = kindSel.value;
  const rows = ROWS.filter((r) => {
    if (u && (r.username || "") !== u) return false;
    if (k && kindOf(r.action) !== k) return false;
    if (q && ![r.action, r.detail, r.ip, r.username].some((v) => (v || "").toLowerCase().includes(q))) return false;
    return true;
  });
  body.innerHTML = rows.map(rowHtml).join("");
  document.getElementById("audit-table-wrap").hidden = !rows.length;
  document.getElementById("audit-nomatch").hidden = !!rows.length;
  document.getElementById("audit-shown").textContent = rows.length === ROWS.length ? `${ROWS.length} events` : `${rows.length} of ${ROWS.length} events`;
}

function renderStats() {
  const users = new Set(ROWS.map((r) => r.username).filter(Boolean));
  const failed = ROWS.filter((r) => /login_failed|locked/.test(r.action || "")).length;
  document.getElementById("audit-stat-users").textContent = users.size;
  document.getElementById("audit-stat-failed").textContent = failed;
  document.getElementById("audit-stat-failed-tile").classList.toggle("is-alert", failed > 0);
  document.getElementById("audit-stat-last").textContent = ROWS.length ? fmtRel(ROWS[0].ts) : "-";
  [...users].sort().forEach((name) => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    userSel.appendChild(o);
  });
}

if (!ROWS.length) {
  document.getElementById("audit-empty").hidden = false;
  document.getElementById("audit-stat-users").textContent = "0";
  document.getElementById("audit-stat-failed").textContent = "0";
} else {
  document.getElementById("audit-toolbar").hidden = false;
  renderStats();
  render();
  search.addEventListener("input", render);
  userSel.addEventListener("change", render);
  kindSel.addEventListener("change", render);
}
