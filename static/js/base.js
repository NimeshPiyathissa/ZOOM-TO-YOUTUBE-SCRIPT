// Global topbar quick-action + status badge, shared across every authenticated page.
(function () {
  const badge = document.getElementById("topbar-state-badge");
  const uptimeEl = document.getElementById("topbar-uptime");
  const goLiveBtns = [document.getElementById("topbar-go-live"), document.getElementById("mobile-go-live")];
  const stopBtns = [document.getElementById("topbar-stop"), document.getElementById("mobile-stop")];
  let lastState = null;

  // Bug fixed here: this used to read `stream.state` (the old 3-value
  // LIVE/ERROR/STOPPED field). Part 1's dashboard-honesty fix replaced
  // that with `stream.phase` (5 values) everywhere else, but this file
  // was missed - since then this badge has shown "badge-inactive" /
  // "undefined" regardless of actual state on every page. This was
  // literally "the top badge" from the original incident report.
  const PHASE_LABEL = {
    STOPPED: "Stopped", STARTING: "Starting…", LIVE: "Live",
    RECONNECTING: "Reconnecting…", FAILED: "Failed",
  };

  function paint(stream) {
    if (!stream) return;
    const phase = stream.phase;
    badge.className = "badge " + phaseBadgeClass(phase);
    badge.innerHTML = `<span class="dot"></span>${PHASE_LABEL[phase] || phase}`;
    uptimeEl.textContent = phase === "LIVE" ? fmtUptime(stream.uptime_seconds) : "";
    const canStop = phase === "LIVE" || phase === "RECONNECTING";
    goLiveBtns.forEach((b) => { if (b) b.hidden = canStop; });
    stopBtns.forEach((b) => { if (b) b.hidden = !canStop; });
    if (lastState && lastState !== phase) announce(`Stream is now ${(PHASE_LABEL[phase] || phase).toLowerCase()}`);
    lastState = phase;
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
