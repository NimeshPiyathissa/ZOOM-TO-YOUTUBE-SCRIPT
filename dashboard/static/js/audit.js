const ROWS = JSON.parse(document.getElementById("audit-rows-data").textContent);

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

if (!ROWS.length) {
  document.getElementById("audit-empty").hidden = false;
} else {
  document.getElementById("audit-table-wrap").hidden = false;
  document.getElementById("audit-body").innerHTML = ROWS.map(r => `
    <tr>
      <td class="mono tnum">${fmtTs(r.ts)}</td>
      <td>${r.username || "-"}</td>
      <td>${r.action}</td>
      <td class="mono">${r.detail || "-"}</td>
      <td class="mono">${r.ip || "-"}</td>
    </tr>`).join("");
}
