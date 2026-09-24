// ============================================================
// YouTube Live Studio Room (/studio)
// Broadcast-grade master controls, tally strip, telemetry,
// dual-channel audio peak meter, RTMP manager, and mobile live chat.
// ============================================================

(function () {
  "use strict";

  // Elements: Master & Tally
  const masterStrip = document.getElementById("studio-tally-strip");
  const tallyLabel = document.getElementById("tally-label");
  const tallyElapsed = document.getElementById("tally-elapsed");
  const masterBtn = document.getElementById("btn-master-action");
  const masterBtnText = document.getElementById("btn-action-text");
  const safetyBackdrop = document.getElementById("safety-modal-backdrop");
  const modalTitle = document.getElementById("safety-modal-title");
  const modalBody = document.getElementById("safety-modal-body");
  const modalConfirmBtn = document.getElementById("modal-confirm-btn");
  const modalCancelBtn = document.getElementById("modal-cancel-btn");
  const modalIcon = document.getElementById("safety-modal-icon");

  // Elements: Telemetry
  const telemBadge = document.getElementById("telem-status-badge");
  const telemBadgeText = document.getElementById("telem-status-text");
  const telemBitrate = document.getElementById("telem-bitrate");
  const telemBitrateSub = document.getElementById("telem-bitrate-sub");
  const telemFps = document.getElementById("telem-fps");
  const telemDrop = document.getElementById("telem-drop");
  const telemDropSub = document.getElementById("telem-drop-sub");
  const telemCpu = document.getElementById("telem-cpu");
  const telemAlert = document.getElementById("telem-alert");
  const telemAlertMsg = document.getElementById("telem-alert-msg");
  const tileBitrate = document.getElementById("tile-bitrate");
  const tileFps = document.getElementById("tile-fps");
  const tileDrop = document.getElementById("tile-drop");
  const tileCpu = document.getElementById("tile-cpu");

  // Elements: Audio Ladder
  const audioBadge = document.getElementById("audio-live-badge");
  const audioBadgeText = document.getElementById("audio-live-text");
  const chLVal = document.getElementById("ch-l-val");
  const chRVal = document.getElementById("ch-r-val");
  const audioRmsVal = document.getElementById("audio-rms-val");
  const audioPeakVal = document.getElementById("audio-peak-val");
  const ladderL = document.getElementById("ladder-l");
  const ladderR = document.getElementById("ladder-r");
  const ledsL = ladderL ? Array.from(ladderL.querySelectorAll(".led-bar")) : [];
  const ledsR = ladderR ? Array.from(ladderR.querySelectorAll(".led-bar")) : [];

  // Elements: Stream Key & RTMP
  const rtmpInput = document.getElementById("rtmp-ingest-url");
  const btnCopyRtmp = document.getElementById("btn-copy-rtmp");
  const skInput = document.getElementById("sk-input");
  const btnRevealSk = document.getElementById("btn-reveal-sk");
  const btnCopySk = document.getElementById("btn-copy-sk");
  const btnSaveSk = document.getElementById("btn-save-sk");
  const skStatusMsg = document.getElementById("sk-status-msg");

  // Elements: Program Monitor (preview)
  const studioPreview = document.getElementById("studio-preview");
  const studioPreviewImg = document.getElementById("studio-preview-img");
  const studioPreviewPlaceholder = document.getElementById("studio-preview-placeholder");
  const studioPreviewState = document.getElementById("studio-preview-state");
  const studioPreviewStateText = document.getElementById("studio-preview-state-text");
  const studioPreviewElapsed = document.getElementById("studio-preview-elapsed");
  const studioPreviewAge = document.getElementById("studio-preview-age");

  // Elements: Source
  const sourceStatusBadge = document.getElementById("source-status-badge");
  const sourceBadgeText = document.getElementById("source-badge-text");
  const sourceFeedName = document.getElementById("source-feed-name");
  const sourceFeedDesc = document.getElementById("source-feed-desc");
  const btnOpenZoom = document.getElementById("btn-open-zoom");
  const btnOpenRemote = document.getElementById("btn-open-remote");

  // Elements: Live Chat Drawer & Links
  const chatDrawer = document.getElementById("chat-drawer");
  const chatBackdrop = document.getElementById("chat-backdrop");
  const btnTopChat = document.getElementById("btn-top-chat");
  const btnCloseChat = document.getElementById("btn-close-chat");
  const btnSaveChatId = document.getElementById("btn-save-chat-id");
  const chatVideoIdInput = document.getElementById("chat-video-id");
  const chatFrameWrap = document.getElementById("chat-frame-wrap");
  const btnChatPopout = document.getElementById("btn-chat-popout");
  const btnPublicWatch = document.getElementById("btn-public-watch");

  // State Tracking
  let currentPhase = "STOPPED"; // STOPPED | STARTING | LIVE | FAILED
  let streamUptime = 0;
  let uptimeClockTimer = null;
  let audioPollTimer = null;
  let statePollTimer = null;
  let previewPollTimer = null;
  let pendingAction = null; // "go-live" | "stop"
  let cachedStreamKey = "";
  let isKeyRevealed = false;
  let peakHoldL = -120;
  let peakHoldR = -120;
  let peakHoldDecay = 0;

  // CSRF token helper
  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // Toast Notification helper
  function showToast(message, type = "info") {
    if (typeof window.toast === "function") {
      window.toast(message, type);
    } else {
      const container = document.getElementById("toasts");
      if (!container) return;
      const el = document.createElement("div");
      el.className = `toast toast-${type}`;
      el.textContent = message;
      container.appendChild(el);
      setTimeout(() => el.remove(), 3500);
    }
  }

  // Formatting helpers
  function formatTimecode(seconds) {
    if (seconds == null || isNaN(seconds) || seconds < 0) return "00:00:00";
    const s = Math.floor(seconds);
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const secs = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
  }

  // ---------------------------------------------------------------- Master Broadcast & Tally

  const PHASE_LABEL = { STOPPED: "Stopped", STARTING: "Starting…", LIVE: "LIVE", RECONNECTING: "Reconnecting…", FAILED: "Failed" };

  function updatePreviewMonitor(phase, uptime) {
    if (!studioPreview) return;
    studioPreview.dataset.phase = phase;
    studioPreviewState.className = "badge panel-state " + phaseBadgeClass(phase);
    studioPreviewStateText.textContent = PHASE_LABEL[phase] || phase;
    studioPreviewElapsed.textContent = phase === "LIVE" ? formatTimecode(uptime) : "";
  }

  function updateTally(phase, uptime) {
    currentPhase = (phase || "STOPPED").toUpperCase();
    streamUptime = uptime || 0;
    updatePreviewMonitor(currentPhase, streamUptime);

    if (currentPhase === "LIVE") {
      masterStrip.setAttribute("data-state", "on-air");
      tallyLabel.textContent = "ON-AIR";
      tallyElapsed.textContent = formatTimecode(streamUptime);
      masterBtn.setAttribute("data-action", "stop");
      masterBtn.disabled = false;
      masterBtnText.textContent = "END STREAM";
      masterBtn.className = "btn btn-studio-action btn-touch btn-danger";
    } else if (currentPhase === "STARTING" || currentPhase === "RECONNECTING") {
      masterStrip.setAttribute("data-state", "starting");
      tallyLabel.textContent = "STARTING";
      tallyElapsed.textContent = "--:--:--";
      masterBtn.setAttribute("data-action", "starting");
      masterBtn.disabled = true;
      masterBtnText.textContent = "STARTING…";
      masterBtn.className = "btn btn-studio-action btn-touch";
    } else {
      masterStrip.setAttribute("data-state", "off-air");
      tallyLabel.textContent = currentPhase === "FAILED" ? "FAILED" : "OFF-AIR";
      tallyElapsed.textContent = "00:00:00";
      masterBtn.setAttribute("data-action", "go-live");
      masterBtn.disabled = false;
      masterBtnText.textContent = "GO LIVE";
      masterBtn.className = "btn btn-studio-action btn-touch";
    }
  }

  // Uptime local clock ticker (ticks every second while LIVE)
  function tickClock() {
    if (currentPhase === "LIVE" && streamUptime >= 0) {
      streamUptime += 1;
      tallyElapsed.textContent = formatTimecode(streamUptime);
    }
  }

  // Safety Confirmation Modal
  function openSafetyModal(action) {
    pendingAction = action;
    if (action === "go-live") {
      modalTitle.textContent = "Confirm Going Live";
      modalBody.textContent =
        "You are about to start the live stream encoder (ffmpeg-stream) and broadcast directly to YouTube Live. Verify that your Zoom or Web Kiosk source is ready.";
      modalConfirmBtn.textContent = "Confirm & Go Live";
      modalConfirmBtn.className = "btn btn-primary btn-touch";
      modalIcon.className = "studio-modal-icon icon-go-live";
    } else if (action === "stop") {
      modalTitle.textContent = "Confirm Ending Stream";
      modalBody.textContent =
        "Are you sure you want to end the broadcast? Stopping the live encoder will terminate the live feed for all YouTube viewers.";
      modalConfirmBtn.textContent = "End Stream Now";
      modalConfirmBtn.className = "btn btn-danger btn-touch";
      modalIcon.className = "studio-modal-icon icon-danger";
    }
    safetyBackdrop.hidden = false;
    modalConfirmBtn.focus();
  }

  function closeSafetyModal() {
    safetyBackdrop.hidden = true;
    pendingAction = null;
  }

  async function executeMasterAction() {
    const action = pendingAction;
    closeSafetyModal();
    if (!action) return;

    masterBtn.disabled = true;
    masterBtnText.textContent = action === "go-live" ? "Starting…" : "Stopping…";

    const endpoint = action === "go-live" ? "/api/stream/go-live" : "/api/stream/stop";
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Action failed");
      showToast(action === "go-live" ? "Starting live broadcast…" : "Stream stopped cleanly", "ok");
      await pollState();
    } catch (err) {
      showToast(err.message, "err");
      masterBtn.disabled = false;
      updateTally(currentPhase, streamUptime);
    }
  }

  masterBtn.addEventListener("click", () => {
    const act = masterBtn.getAttribute("data-action");
    if (act === "go-live" || act === "stop") {
      openSafetyModal(act);
    }
  });

  modalConfirmBtn.addEventListener("click", executeMasterAction);
  modalCancelBtn.addEventListener("click", closeSafetyModal);
  safetyBackdrop.addEventListener("click", (e) => {
    if (e.target === safetyBackdrop) closeSafetyModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !safetyBackdrop.hidden) {
      closeSafetyModal();
    }
  });

  // ---------------------------------------------------------------- Telemetry Grid

  function updateTelemetry(stream, ffmpeg, system) {
    const isLive = stream && stream.phase === "LIVE";

    if (isLive) {
      telemBadge.className = "badge badge-live";
      telemBadgeText.textContent = "Broadcasting Live";
    } else if (stream && stream.phase === "STARTING") {
      telemBadge.className = "badge badge-warn";
      telemBadgeText.textContent = "Encoder Starting";
    } else if (stream && stream.phase === "FAILED") {
      telemBadge.className = "badge badge-error";
      telemBadgeText.textContent = "Encoder Failed";
    } else {
      telemBadge.className = "badge badge-inactive";
      telemBadgeText.textContent = "Standby (Off-Air)";
    }

    if (ffmpeg && isLive) {
      // Bitrate
      const kbps = Math.round(ffmpeg.bitrate_kbps || 0);
      telemBitrate.textContent = kbps ? kbps.toLocaleString() : "—";
      if (kbps >= 4500) {
        tileBitrate.className = "telemetry-tile is-healthy";
        telemBitrateSub.textContent = "Healthy ingest bandwidth";
      } else if (kbps > 0) {
        tileBitrate.className = "telemetry-tile is-warn";
        telemBitrateSub.textContent = "Low bitrate (< 4500 kbps)";
      } else {
        tileBitrate.className = "telemetry-tile";
        telemBitrateSub.textContent = "Target 6000 kbps";
      }

      // FPS
      const fps = ffmpeg.fps != null ? ffmpeg.fps.toFixed(1) : "—";
      telemFps.textContent = fps;
      if (ffmpeg.fps >= 29.0) {
        tileFps.className = "telemetry-tile is-healthy";
      } else if (ffmpeg.fps >= 24.0) {
        tileFps.className = "telemetry-tile is-warn";
      } else {
        tileFps.className = "telemetry-tile is-bad";
      }

      // Dropped frames
      const drop = ffmpeg.drop || 0;
      const totalFrames = (ffmpeg.frame || 0) + drop;
      const lossPct = totalFrames > 0 ? ((drop / totalFrames) * 100).toFixed(2) : "0.00";
      telemDrop.textContent = drop.toLocaleString();
      telemDropSub.textContent = `${lossPct}% frame loss`;
      if (drop === 0) {
        tileDrop.className = "telemetry-tile is-healthy";
      } else if (parseFloat(lossPct) < 1.0) {
        tileDrop.className = "telemetry-tile is-warn";
      } else {
        tileDrop.className = "telemetry-tile is-bad";
      }

      // Encoder CPU
      let cpuVal = ffmpeg.encoder_cpu;
      if (cpuVal == null && system && system.cpu_avg != null) {
        cpuVal = system.cpu_avg;
        telemCpu.textContent = cpuVal.toFixed(1);
        document.getElementById("telem-cpu-sub").textContent = "System CPU avg";
      } else if (cpuVal != null) {
        telemCpu.textContent = cpuVal.toFixed(1);
        document.getElementById("telem-cpu-sub").textContent = "ffmpeg process";
      } else {
        telemCpu.textContent = "—";
      }

      if (cpuVal != null) {
        if (cpuVal < 70) tileCpu.className = "telemetry-tile is-healthy";
        else if (cpuVal < 90) tileCpu.className = "telemetry-tile is-warn";
        else tileCpu.className = "telemetry-tile is-bad";
      }

      // Warning
      if (ffmpeg.warning) {
        telemAlert.hidden = false;
        telemAlertMsg.textContent = "Encoder speed below 1.0x - real-time encoding is falling behind.";
      } else {
        telemAlert.hidden = true;
      }
    } else {
      telemBitrate.textContent = "—";
      telemFps.textContent = "—";
      telemDrop.textContent = "0";
      telemDropSub.textContent = "0.0% loss";
      telemCpu.textContent = system && system.cpu_avg ? system.cpu_avg.toFixed(1) : "—";
      tileBitrate.className = "telemetry-tile";
      tileFps.className = "telemetry-tile";
      tileDrop.className = "telemetry-tile";
      tileCpu.className = "telemetry-tile";

      if (stream && stream.phase === "FAILED" && stream.last_error) {
        telemAlert.hidden = false;
        telemAlertMsg.textContent = stream.cause
          ? `${stream.cause}: ${stream.last_error}`
          : stream.last_error;
      } else {
        telemAlert.hidden = true;
      }
    }
  }

  // ---------------------------------------------------------------- Dual-Channel LED Audio Peak Ladder

  function paintLedLadder(leds, peakDb) {
    if (!leds || !leds.length) return;
    const isLive = peakDb != null && peakDb > -100;
    leds.forEach((bar) => {
      const threshold = parseFloat(bar.getAttribute("data-db"));
      const lit = isLive && peakDb >= threshold;
      bar.classList.toggle("is-lit", lit);
    });
  }

  async function pollAudio() {
    if (document.hidden) return;
    try {
      const res = await fetch("/api/audio/level");
      if (!res.ok) return;
      const data = await res.json();

      if (data.live) {
        audioBadge.className = "badge badge-live";
        audioBadgeText.textContent = "Sink Active";

        const peakDb = data.peak_db != null ? data.peak_db : -120;
        const peakL = data.peak_l_db != null ? data.peak_l_db : peakDb;
        const peakR = data.peak_r_db != null ? data.peak_r_db : peakDb;
        const rmsDb = data.rms_db != null ? data.rms_db : -120;

        // Peak Hold logic
        if (peakL > peakHoldL || peakHoldDecay <= 0) {
          peakHoldL = peakL;
          peakHoldDecay = 8;
        } else {
          peakHoldDecay -= 1;
          peakHoldL = Math.max(-120, peakHoldL - 1.5);
        }
        if (peakR > peakHoldR) peakHoldR = peakR;

        // Paint LED Ladders
        paintLedLadder(ledsL, peakL);
        paintLedLadder(ledsR, peakR);

        // Numeric Readouts
        chLVal.textContent = peakL > -90 ? `${peakL.toFixed(1)} dB` : "-∞";
        chRVal.textContent = peakR > -90 ? `${peakR.toFixed(1)} dB` : "-∞";
        audioRmsVal.textContent = rmsDb > -90 ? `${rmsDb.toFixed(1)} dBFS` : "—";
        const maxPeak = Math.max(peakL, peakR);
        audioPeakVal.textContent = maxPeak > -90 ? `${maxPeak.toFixed(1)} dBFS` : "—";

        // Clip / Alert styling
        if (maxPeak >= -0.5) {
          audioBadge.className = "badge badge-error";
          audioBadgeText.textContent = "PEAK CLIPPING";
        }
      } else {
        audioBadge.className = "badge badge-inactive";
        audioBadgeText.textContent = data.age_seconds == null ? "Sampler Standby" : "No Signal";
        paintLedLadder(ledsL, -120);
        paintLedLadder(ledsR, -120);
        chLVal.textContent = "-∞";
        chRVal.textContent = "-∞";
        audioRmsVal.textContent = "—";
        audioPeakVal.textContent = "—";
      }
    } catch (e) {
      // Ignore transient network errors
    }
  }

  // ---------------------------------------------------------------- Stream Key & RTMP Manager

  btnCopyRtmp.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(rtmpInput.value);
      showToast("RTMP Ingest URL copied to clipboard!", "ok");
    } catch (err) {
      rtmpInput.select();
      document.execCommand("copy");
      showToast("RTMP Ingest URL copied!", "ok");
    }
  });

  btnRevealSk.addEventListener("click", async () => {
    if (isKeyRevealed) {
      skInput.type = "password";
      isKeyRevealed = false;
      btnRevealSk.setAttribute("data-tooltip", "Reveal stream key");
      return;
    }

    if (cachedStreamKey) {
      skInput.type = "text";
      skInput.value = cachedStreamKey;
      isKeyRevealed = true;
      btnRevealSk.setAttribute("data-tooltip", "Hide stream key");
      return;
    }

    try {
      btnRevealSk.disabled = true;
      const res = await fetch("/api/studio/stream-key/reveal");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to reveal key");
      cachedStreamKey = data.stream_key || "";
      skInput.type = "text";
      skInput.value = cachedStreamKey;
      isKeyRevealed = true;
      btnRevealSk.setAttribute("data-tooltip", "Hide stream key");
    } catch (err) {
      showToast(err.message, "err");
    } finally {
      btnRevealSk.disabled = false;
    }
  });

  btnCopySk.addEventListener("click", async () => {
    let keyToCopy = skInput.value || cachedStreamKey;
    if (!keyToCopy) {
      try {
        const res = await fetch("/api/studio/stream-key/reveal");
        const data = await res.json();
        if (res.ok && data.stream_key) {
          keyToCopy = data.stream_key;
          cachedStreamKey = data.stream_key;
        }
      } catch (e) {
        // Fallback
      }
    }

    if (!keyToCopy) {
      showToast("No stream key is configured.", "warn");
      return;
    }

    try {
      await navigator.clipboard.writeText(keyToCopy);
      showToast("Stream Key copied to clipboard!", "ok");
    } catch (err) {
      skInput.type = "text";
      skInput.value = keyToCopy;
      skInput.select();
      document.execCommand("copy");
      skInput.type = isKeyRevealed ? "text" : "password";
      showToast("Stream Key copied!", "ok");
    }
  });

  skInput.addEventListener("input", () => {
    btnSaveSk.disabled = skInput.value.trim().length === 0;
  });

  btnSaveSk.addEventListener("click", async () => {
    const newKey = skInput.value.trim();
    if (!newKey) return;

    btnSaveSk.disabled = true;
    btnSaveSk.textContent = "Saving…";
    try {
      const res = await fetch("/api/studio/stream-key", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ stream_key: newKey }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to save key");

      cachedStreamKey = newKey;
      skInput.placeholder = data.hint || "••••••••••••";
      skStatusMsg.textContent = "Key updated and saved to .env!";
      showToast("Stream Key saved to .env!", "ok");
    } catch (err) {
      showToast(err.message, "err");
    } finally {
      btnSaveSk.disabled = false;
      btnSaveSk.innerHTML = '<svg class="icon icon-sm"><use href="#i-check"></use></svg>Save Key to .env';
    }
  });

  // ---------------------------------------------------------------- Source Indicator & Redirection

  function updateSourceIndicator(activeSource) {
    if (!activeSource) {
      sourceStatusBadge.className = "source-status-badge";
      sourceBadgeText.textContent = "No Source Selected";
      sourceFeedName.textContent = "Default Pipeline Display (:99)";
      sourceFeedDesc.textContent = "Select a Zoom meeting or Web Kiosk source to stream.";
      btnOpenZoom.classList.remove("btn-primary");
      btnOpenRemote.classList.remove("btn-primary");
      return;
    }

    sourceFeedName.textContent = activeSource.name || "Active Program";
    if (activeSource.type === "zoom") {
      sourceStatusBadge.className = "source-status-badge source-zoom";
      sourceBadgeText.textContent = "Zoom Meeting Source";
      sourceFeedDesc.textContent = "Live video and audio fed directly from the Zoom Linux/Web client.";
      btnOpenZoom.classList.add("btn-primary");
      btnOpenRemote.classList.remove("btn-primary");
    } else {
      sourceStatusBadge.className = "source-status-badge source-web";
      sourceBadgeText.textContent = activeSource.type === "webpage" ? "Web Kiosk Source" : "Direct Media Source";
      sourceFeedDesc.textContent = "Live video and audio fed from Chromium kiosk / direct stream URL.";
      btnOpenRemote.classList.add("btn-primary");
      btnOpenZoom.classList.remove("btn-primary");
    }
  }

  // ---------------------------------------------------------------- Slide-Over YouTube Live Chat

  function toggleLiveChat(open) {
    const willOpen = open != null ? open : !chatDrawer.classList.contains("is-open");
    chatDrawer.classList.toggle("is-open", willOpen);
    chatBackdrop.hidden = !willOpen;
    chatDrawer.setAttribute("aria-hidden", String(!willOpen));
    btnTopChat.setAttribute("aria-expanded", String(willOpen));
  }

  btnTopChat.addEventListener("click", () => toggleLiveChat());
  btnCloseChat.addEventListener("click", () => toggleLiveChat(false));
  chatBackdrop.addEventListener("click", () => toggleLiveChat(false));

  btnSaveChatId.addEventListener("click", async () => {
    const val = chatVideoIdInput.value.trim();
    btnSaveChatId.disabled = true;
    try {
      const res = await fetch("/api/studio/settings", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ video_id: val }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to update settings");

      showToast("YouTube Broadcast connected!", "ok");
      updateChatEmbed(data.video_id, data.watch_url);
    } catch (err) {
      showToast(err.message, "err");
    } finally {
      btnSaveChatId.disabled = false;
    }
  });

  function updateChatEmbed(videoId, watchUrl) {
    if (videoId) {
      const domain = window.location.hostname;
      chatFrameWrap.innerHTML = `
        <iframe
          id="chat-iframe"
          class="chat-iframe"
          src="https://www.youtube.com/live_chat?v=${encodeURIComponent(videoId)}&embed_domain=${encodeURIComponent(domain)}"
          title="YouTube Live Chat"
          loading="lazy">
        </iframe>
      `;
      btnChatPopout.href = `https://www.youtube.com/live_chat?v=${encodeURIComponent(videoId)}`;
      btnChatPopout.hidden = false;
      if (btnPublicWatch && watchUrl) {
        btnPublicWatch.href = watchUrl;
      }
    } else {
      chatFrameWrap.innerHTML = `
        <div class="chat-placeholder" id="chat-placeholder">
          <svg class="icon"><use href="#i-message-square"></use></svg>
          <p class="chat-placeholder-title">No Live Chat Connected</p>
          <p class="hint">Enter your broadcast Video ID or Watch URL above to view the live audience chat, or launch YouTube Live Control Room.</p>
        </div>
      `;
      btnChatPopout.hidden = true;
    }
  }

  // ---------------------------------------------------------------- Program Monitor preview poll

  async function pollPreview() {
    if (document.hidden || !studioPreview) return;
    try {
      const res = await fetch(`/api/preview.jpg?t=${Date.now()}`, { credentials: "same-origin" });
      if (!res.ok) throw new Error("no preview");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const old = studioPreviewImg.src;
      studioPreviewImg.src = url;
      studioPreviewImg.hidden = false;
      studioPreviewPlaceholder.hidden = true;
      studioPreview.classList.remove("is-stale");
      studioPreviewAge.textContent = "live";
      studioPreview.dataset.lastOk = String(Date.now());
      if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
    } catch (err) {
      const last = Number(studioPreview.dataset.lastOk || 0);
      if (last && Date.now() - last < 30000) {
        studioPreview.classList.add("is-stale");
        studioPreviewAge.textContent = "stale " + Math.round((Date.now() - last) / 1000) + "s";
      } else {
        studioPreviewImg.hidden = true;
        studioPreviewPlaceholder.hidden = false;
        studioPreviewAge.textContent = "";
      }
    }
  }

  // ---------------------------------------------------------------- Polling & Lifecycle

  async function pollState() {
    try {
      const res = await fetch("/api/state");
      if (!res.ok) return;
      const data = await res.json();

      updateTally(data.stream ? data.stream.phase : "STOPPED", data.stream ? data.stream.uptime_seconds : 0);
      updateTelemetry(data.stream, data.ffmpeg, data.system);
      updateSourceIndicator(data.active_source);
    } catch (e) {
      // Ignore transient errors
    }
  }

  // Initialize
  function init() {
    // Initial data from script tags
    try {
      const settingsEl = document.getElementById("studio-settings-data");
      if (settingsEl) {
        const settings = JSON.parse(settingsEl.textContent);
        if (settings.video_id) {
          updateChatEmbed(settings.video_id, settings.watch_url);
        }
      }
      const sourceEl = document.getElementById("active-source-init");
      if (sourceEl) {
        const initialSource = JSON.parse(sourceEl.textContent);
        updateSourceIndicator(initialSource);
      }
    } catch (e) {
      console.warn("Error parsing initial studio data", e);
    }

    // Elements: BRB Slate, Panic Mute, Local Recording
    const btnStudioBrb = document.getElementById("btn-studio-brb");
    const btnBrbText = document.getElementById("btn-brb-text");
    const btnPanicMute = document.getElementById("btn-panic-mute");
    const btnPanicText = document.getElementById("btn-panic-text");
    const btnToggleRecording = document.getElementById("btn-toggle-recording");
    const btnToggleRecText = document.getElementById("btn-toggle-rec-text");
    const recStatusBadge = document.getElementById("rec-status-badge");
    const recStatusText = document.getElementById("rec-status-text");
    const recDuration = document.getElementById("rec-duration");
    const recSize = document.getElementById("rec-size");
    const recFreeDisk = document.getElementById("rec-free-disk");
    const recFileName = document.getElementById("rec-file-name");
    const recAlert = document.getElementById("rec-alert");
    const recAlertMsg = document.getElementById("rec-alert-msg");

    let brbActive = false;
    async function pollBrb() {
      try {
        const res = await fetch("/api/slate/brb");
        if (res.ok) {
          const data = await res.json();
          brbActive = !!data.active;
          if (btnStudioBrb) {
            btnStudioBrb.className = `btn btn-touch ${brbActive ? "btn-danger" : "btn-secondary"}`;
          }
          if (btnBrbText) {
            btnBrbText.textContent = brbActive ? "SLATE ACTIVE" : "BRB Slate";
          }
        }
      } catch (e) {}
    }

    if (btnStudioBrb) {
      btnStudioBrb.addEventListener("click", async () => {
        try {
          const res = await fetch("/api/slate/brb", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": getCsrfToken(),
            },
            body: JSON.stringify({ action: "toggle" }),
          });
          if (res.ok) {
            const data = await res.json();
            brbActive = !!(data.state && data.state.active);
            if (btnStudioBrb) {
              btnStudioBrb.className = `btn btn-touch ${brbActive ? "btn-danger" : "btn-secondary"}`;
            }
            if (btnBrbText) {
              btnBrbText.textContent = brbActive ? "SLATE ACTIVE" : "BRB Slate";
            }
            if (typeof window.toast === "function") {
              window.toast(brbActive ? "Emergency BRB Holding Card Active" : "BRB Slate Cleared (Live Program)", brbActive ? "warning" : "info");
            }
          }
        } catch (e) {
          console.warn("BRB toggle error", e);
        }
      });
    }

    // Clean Feed Button Trigger
    const btnCleanFeed = document.getElementById("btn-clean-feed");
    const btnCleanFeedText = document.getElementById("btn-clean-feed-text");
    const btnSourceCleanFeed = document.getElementById("btn-source-clean-feed");

    async function triggerCleanFeed(btn) {
      if (btn) btn.disabled = true;
      if (btnCleanFeedText) btnCleanFeedText.textContent = "Cleaning…";
      try {
        const res = await fetch("/api/zoom/clean-feed", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": getCsrfToken(),
          },
        });
        const data = await res.json();
        if (data.ok) {
          showToast("Clean feed applied! Zoom controls and black borders removed.", "ok");
        } else {
          showToast(data.error || data.detail || "Clean feed trigger failed", "err");
        }
      } catch (err) {
        showToast("Error triggering clean feed: " + err.message, "err");
      } finally {
        if (btnCleanFeedText) btnCleanFeedText.textContent = "Clean Feed";
        setTimeout(() => {
          if (btn) btn.disabled = false;
        }, 1000);
      }
    }

    if (btnCleanFeed) {
      btnCleanFeed.addEventListener("click", () => triggerCleanFeed(btnCleanFeed));
    }
    if (btnSourceCleanFeed) {
      btnSourceCleanFeed.addEventListener("click", () => triggerCleanFeed(btnSourceCleanFeed));
    }

    let isPanicMuted = false;
    async function pollPanicMute() {
      try {
        const res = await fetch("/api/audio/stream");
        if (res.ok) {
          const data = await res.json();
          isPanicMuted = !!data.muted;
          if (btnPanicMute) {
            btnPanicMute.className = `btn btn-touch ${isPanicMuted ? "btn-danger" : "btn-secondary"}`;
          }
          if (btnPanicText) {
            btnPanicText.textContent = isPanicMuted ? "PANIC MUTED" : "Panic Mute";
          }
        }
      } catch (e) {}
    }

    if (btnPanicMute) {
      btnPanicMute.addEventListener("click", async () => {
        try {
          const action = isPanicMuted ? "unmute" : "mute";
          const res = await fetch("/api/audio/stream", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": getCsrfToken(),
            },
            body: JSON.stringify({ action }),
          });
          if (res.ok) {
            const data = await res.json();
            isPanicMuted = !!data.muted;
            if (btnPanicMute) {
              btnPanicMute.className = `btn btn-touch ${isPanicMuted ? "btn-danger" : "btn-secondary"}`;
            }
            if (btnPanicText) {
              btnPanicText.textContent = isPanicMuted ? "PANIC MUTED" : "Panic Mute";
            }
            if (typeof window.toast === "function") {
              window.toast(isPanicMuted ? "Master Audio Panic Muted (Monitor sink silenced)" : "Master Audio Restored", isPanicMuted ? "error" : "success");
            }
          }
        } catch (e) {
          console.warn("Panic mute error", e);
        }
      });
    }

    // Master recording controls next to Go Live
    const btnStudioRecord = document.getElementById("btn-studio-record");
    const studioRecText = document.getElementById("studio-rec-text");
    const studioRecIcon = document.getElementById("studio-rec-icon");
    const studioRecPill = document.getElementById("studio-rec-pill");
    const studioRecTimer = document.getElementById("studio-rec-timer");

    let isRecording = false;
    async function pollRecording() {
      try {
        const res = await fetch("/api/record");
        if (res.ok) {
          const data = await res.json();
          isRecording = !!data.recording;

          const dur = parseInt(data.duration, 10) || 0;
          const h = String(Math.floor(dur / 3600)).padStart(2, "0");
          const m = String(Math.floor((dur % 3600) / 60)).padStart(2, "0");
          const s = String(dur % 60).padStart(2, "0");
          const timeStr = `${h}:${m}:${s}`;

          // Update master control next to Go Live
          if (btnStudioRecord) {
            btnStudioRecord.className = `btn btn-touch ${isRecording ? "btn-rec-active" : "btn-rec"}`;
          }
          if (studioRecIcon) {
            studioRecIcon.textContent = isRecording ? "⏹" : "⏺";
          }
          if (studioRecText) {
            studioRecText.textContent = isRecording ? "Stop Recording" : "Start Recording";
          }
          if (studioRecPill) {
            studioRecPill.style.display = isRecording ? "inline-flex" : "none";
          }
          if (studioRecTimer) {
            studioRecTimer.textContent = timeStr;
          }

          // Update recording card details
          if (recStatusBadge) {
            recStatusBadge.className = `badge ${isRecording ? "badge-live" : "badge-inactive"}`;
          }
          if (recStatusText) {
            recStatusText.textContent = isRecording ? "RECORDING" : "Standby";
          }
          if (btnToggleRecording) {
            btnToggleRecording.className = `btn btn-sm btn-touch ${isRecording ? "btn-danger" : "btn-secondary"}`;
          }
          if (btnToggleRecText) {
            btnToggleRecText.textContent = isRecording ? "Stop Recording" : "Start Recording";
          }
          if (recDuration) {
            recDuration.textContent = timeStr;
          }
          if (recSize) {
            recSize.textContent = (parseFloat(data.size_mb) || 0).toFixed(1);
          }
          if (recFreeDisk) {
            recFreeDisk.textContent = (parseFloat(data.free_gb) || 0).toFixed(2);
          }
          if (recFileName) {
            const fn = (data.file || "").split("/").pop() || (isRecording ? "rec_active.mp4" : "No active file");
            recFileName.textContent = fn;
          }
          if (recAlert && recAlertMsg) {
            if (data.halted_reason === "DISK_LOW_SAFETY_HALT") {
              recAlertMsg.textContent = "Recording stopped automatically: Free disk space fell below safety threshold (2.0 GB).";
              recAlert.hidden = false;
            } else if (parseFloat(data.free_gb) < 2.0 && parseFloat(data.free_gb) > 0) {
              recAlertMsg.textContent = `Warning: Free disk space low (${data.free_gb} GB). Recording will safety-halt below 2.0 GB.`;
              recAlert.hidden = false;
            } else {
              recAlert.hidden = true;
            }
          }
        }
      } catch (e) {}
    }

    async function toggleRecordingAction(btnElement) {
      try {
        if (btnElement) btnElement.disabled = true;
        const action = isRecording ? "stop" : "start";
        const res = await fetch("/api/record", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": getCsrfToken(),
          },
          body: JSON.stringify({ action }),
        });
        if (res.ok) {
          await pollRecording();
          if (typeof window.toast === "function") {
            window.toast(isRecording ? "Local recording started (/home/dashboard/recordings/)" : "Local recording finalized", isRecording ? "success" : "info");
          }
        } else {
          const err = await res.json();
          if (typeof window.toast === "function") {
            window.toast(err.detail || "Recording error", "error");
          }
        }
      } catch (e) {
        console.warn("Toggle recording error", e);
      } finally {
        if (btnElement) btnElement.disabled = false;
      }
    }

    if (btnStudioRecord) {
      btnStudioRecord.addEventListener("click", () => toggleRecordingAction(btnStudioRecord));
    }
    if (btnToggleRecording) {
      btnToggleRecording.addEventListener("click", () => toggleRecordingAction(btnToggleRecording));
    }

    // Run initial state poll
    pollState();
    pollAudio();
    pollPreview();
    pollBrb();
    pollPanicMute();
    pollRecording();

    // Start background intervals
    uptimeClockTimer = setInterval(tickClock, 1000);
    statePollTimer = setInterval(pollState, 2000);
    audioPollTimer = setInterval(pollAudio, 250);
    previewPollTimer = setInterval(pollPreview, 3000);
    setInterval(pollBrb, 3000);
    setInterval(pollPanicMute, 3000);
    setInterval(pollRecording, 3000);

    // Visibility management
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        pollState();
        pollAudio();
        pollPreview();
        pollBrb();
        pollPanicMute();
        pollRecording();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
