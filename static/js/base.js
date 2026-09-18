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
  const restartBtn = document.getElementById("mobile-restart");
  if (restartBtn) restartBtn.addEventListener("click", () => quickAction("restart", "Restart the encoder? This briefly interrupts the live stream."));

  // ---- remote-desktop overlay: the escape hatch, one tap from anywhere.
  // The noVNC client is only fetched (dynamic import) and connected when
  // the overlay is actually opened, and disconnected on close.
  const overlay = document.getElementById("vnc-overlay");
  let rfb = null, releaseFocus = null, opener = null;
  async function openDesktop(e) {
    if (!overlay) return;
    opener = e && e.currentTarget;
    overlay.hidden = false;
    document.body.classList.add("vnc-overlay-open");
    document.getElementById("vnc-overlay-status").textContent = "connecting…";
    const screen = document.getElementById("vnc-overlay-screen");
    screen.innerHTML = "";
    releaseFocus = trapFocus(overlay, closeDesktop);
    document.getElementById("vnc-overlay-close").focus();
    try {
      const mod = await import("/static/js/vnc-embed.js");
      rfb = mod.connectVnc(screen, { onDisconnect: () => { document.getElementById("vnc-overlay-status").textContent = "disconnected"; } });
      rfb.addEventListener("connect", () => { document.getElementById("vnc-overlay-status").textContent = "connected · touch to control"; });
      announce("Remote desktop opened");
    } catch (err) { document.getElementById("vnc-overlay-status").textContent = "could not load the viewer"; }
  }
  function closeDesktop() {
    if (!overlay || overlay.hidden) return;
    if (rfb) { try { rfb.disconnect(); } catch (err) { /* already gone */ } rfb = null; }
    if (releaseFocus) { releaseFocus(); releaseFocus = null; }
    overlay.hidden = true;
    document.body.classList.remove("vnc-overlay-open");
    if (opener && opener.focus) opener.focus();
    announce("Remote desktop closed");
  }
  ["topbar-desktop", "mobile-desktop"].forEach((id) => { const b = document.getElementById(id); if (b) b.addEventListener("click", openDesktop); });
  const closeBtn = document.getElementById("vnc-overlay-close");
  if (closeBtn) closeBtn.addEventListener("click", closeDesktop);
  window.openRemoteDesktop = openDesktop;

  async function poll() {
    try { const data = await apiFetch("/api/state"); paint(data.stream); window.dispatchEvent(new CustomEvent("zsdash:state", { detail: data })); }
    catch (err) { /* transient */ }
  }
  poll();
  setInterval(poll, 3000);
})();
