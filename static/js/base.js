// Global topbar quick-action + status badge, shared across every authenticated page.
(function () {
  const badge = document.getElementById("topbar-state-badge");
  const uptimeEl = document.getElementById("topbar-uptime");
  const goLiveBtns = [document.getElementById("topbar-go-live"), document.getElementById("mobile-go-live")];
  const stopBtns = [document.getElementById("topbar-stop"), document.getElementById("mobile-stop")];
  let lastState = null;

  function paint(stream) {
    if (!stream) return;
    const map = { LIVE: ["badge-active", "Live"], ERROR: ["badge-failed", "Error"], STOPPED: ["badge-inactive", "Stopped"] };
    const [cls, label] = map[stream.state] || ["badge-inactive", stream.state];
    badge.className = "badge " + cls;
    badge.innerHTML = `<span class="dot"></span>${label}`;
    uptimeEl.textContent = stream.state === "LIVE" ? fmtUptime(stream.uptime_seconds) : "";
    const live = stream.state === "LIVE";
    goLiveBtns.forEach((b) => { if (b) b.hidden = live; });
    stopBtns.forEach((b) => { if (b) b.hidden = !live; });
    if (lastState && lastState !== stream.state) announce(`Stream is now ${stream.state.toLowerCase()}`);
    lastState = stream.state;
  }

  async function quickAction(action, confirmMsg) {
    if (confirmMsg && !(await confirmDialog(confirmMsg, { danger: action === "stop" }))) return;
    try { await apiFetch(`/api/stream/${action}`, { method: "POST" }); toast(`Stream: ${action} sent`); }
    catch (err) { toast(err.message, "err"); }
  }
  goLiveBtns.forEach((b) => b && b.addEventListener("click", () => quickAction("go-live")));
  stopBtns.forEach((b) => b && b.addEventListener("click", () => quickAction("stop", "Stop the live YouTube stream now?")));

  async function poll() {
    try { const data = await apiFetch("/api/state"); paint(data.stream); window.dispatchEvent(new CustomEvent("zsdash:state", { detail: data })); }
    catch (err) { /* transient */ }
  }
  poll();
  setInterval(poll, 3000);
})();
