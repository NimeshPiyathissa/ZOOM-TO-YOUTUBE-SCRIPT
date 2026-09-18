const PRESETS = JSON.parse(document.getElementById("presets-data")?.textContent || "{}");

// ---------------------------------------------------------------- sources (Change 1)

const srcType = document.getElementById("src-type-group");
const srcUrl = document.getElementById("src-url");
const srcDetect = document.getElementById("src-detect");
let currentType = "webpage";

function setSourceType(type, { fromDetect = false } = {}) {
  currentType = type;
  srcType.querySelectorAll(".seg-btn").forEach(b => b.classList.toggle("is-active", b.dataset.type === type));
  document.querySelectorAll(".source-type-fields").forEach(el => { el.hidden = el.id !== `src-fields-${type}`; });
}
if (srcType) {
  srcType.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg-btn");
    if (btn) setSourceType(btn.dataset.type);
  });
  setSourceType("webpage");
}

let detectTimer = null;
if (srcUrl) {
  srcUrl.addEventListener("input", () => {
    clearTimeout(detectTimer);
    const url = srcUrl.value.trim();
    if (!url) { srcDetect.textContent = ""; return; }
    detectTimer = setTimeout(async () => {
      try {
        const res = await apiFetch("/api/sources/detect-type", { method: "POST", body: JSON.stringify({ url }) });
        srcDetect.textContent = `Detected: ${res.type} (change the type above if that's wrong)`;
        setSourceType(res.type, { fromDetect: true });
        if (res.type === "zoom") await explainZoomLink(url);
      } catch (err) { srcDetect.textContent = ""; }
    }, 400);
  });
}

// Zoom links come in three very different kinds - see app/zoomlink.py.
// Explain which one was pasted before the admin saves it.
const ZOOM_KIND_TEXT = {
  meeting: "Ordinary join link - the bot can join this directly.",
  personal: "Personal join link (has a tk= registrant token) - the bot can join directly. Note: tk= tokens are per-registrant and can expire.",
  registration: "This is a webinar REGISTRATION page, not a join link. Save it anyway: on the Remote page you'll open the form on the remote desktop, register by hand, then paste the personal join link Zoom gives you.",
  unknown: "Not recognised as a Zoom join or registration link.",
};
async function explainZoomLink(url) {
  const box = document.getElementById("src-zoom-kind");
  try {
    const info = await apiFetch("/api/zoom/classify", { method: "POST", body: JSON.stringify({ url }) });
    document.getElementById("src-zoom-kind-text").textContent = ZOOM_KIND_TEXT[info.kind] || ZOOM_KIND_TEXT.unknown;
    box.hidden = false;
  } catch (err) { box.hidden = true; }
}

initSegmented(document.getElementById("src-zoom-signin-group"));
initSegmented(document.getElementById("src-direct-mode-group"));

let lastProbe = null;
const probeBtn = document.getElementById("src-direct-probe");
if (probeBtn) {
  probeBtn.onclick = async () => {
    const url = srcUrl.value.trim();
    const resultEl = document.getElementById("src-direct-probe-result");
    if (!url) { toast("Enter a URL first", "err"); return; }
    await withLoading(probeBtn, async () => {
      try {
        const res = await apiFetch("/api/sources/probe", { method: "POST", body: JSON.stringify({ url }) });
        lastProbe = res;
        const v = res.video ? `${res.video.codec} ${res.video.width}x${res.video.height}${res.video.fps ? ' @' + res.video.fps + 'fps' : ''}` : "no video";
        const a = res.audio ? `${res.audio.codec}` : "no audio";
        resultEl.textContent = `${v} · ${a}${res.bitrate_kbps ? ' · ' + res.bitrate_kbps + 'kbps' : ''}`;
        const copyBtn = document.querySelector('#src-direct-mode-group .seg-btn[data-value="copy"]');
        const hint = document.getElementById("src-direct-mode-hint");
        if (copyBtn) {
          copyBtn.disabled = !res.copy_safe;
          hint.textContent = res.copy_safe
            ? "Codecs look YouTube-compatible - \"copy\" is now available (no re-encode, lowest CPU)."
            : "Codecs aren't YouTube-compatible as-is - re-encode is required.";
        }
        toast("Probe complete");
      } catch (err) { toast(err.message, "err"); resultEl.textContent = ""; }
    });
  };
}

document.getElementById("add-source").onclick = async (e) => {
  const name = document.getElementById("src-name").value;
  const url = srcUrl.value;
  let options = {};
  if (currentType === "zoom") {
    options = {
      passcode: document.getElementById("src-zoom-passcode").value,
      bot_name: document.getElementById("src-zoom-botname").value,
      signin_mode: document.querySelector("#src-zoom-signin-group .seg-btn.is-active")?.dataset.value || "guest",
    };
  } else if (currentType === "webpage") {
    options = {
      zoom_level: parseFloat(document.getElementById("src-webpage-zoom").value) || 1.0,
      reload_seconds: parseInt(document.getElementById("src-webpage-reload").value, 10) || 0,
      click_to_start: document.getElementById("src-webpage-click").checked,
    };
  } else {
    options = {
      mode: document.querySelector("#src-direct-mode-group .seg-btn.is-active")?.dataset.value || "reencode",
      loop: document.getElementById("src-direct-loop").checked,
      reconnect: document.getElementById("src-direct-reconnect").checked,
    };
  }
  const account_id = document.getElementById("src-account").value || null;
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/sources", { method: "POST", body: JSON.stringify({ name, type: currentType, url, options, account_id }) });
      toast("Source added"); location.reload();
    } catch (err) { toast(err.message, "err"); }
  });
};

document.querySelectorAll(".switch-source").forEach(btn => btn.addEventListener("click", async () => {
  let preview = {};
  try { preview = await apiFetch(`/api/sources/${btn.dataset.id}/switch-preview`); } catch (err) { /* generic confirm below */ }
  const msg = preview.rtmp_would_drop
    ? "Switch to this source? This kind of switch restarts the encoder (a few seconds of buffering for viewers)."
    : preview.ffmpeg_up ? "Switch to this source while live? The stream stays connected; viewers see a brief slate."
    : "Switch to this source and go live with it?";
  if (!(await confirmDialog(msg, { danger: !!preview.rtmp_would_drop }))) return;
  await withLoading(btn, async () => {
    try {
      const r = await apiFetch(`/api/sources/${btn.dataset.id}/switch`, { method: "POST" });
      toast(r.hot_swapped ? "Switched in place" : r.rtmp_dropped ? "Switched - encoder restarted" : "Switched - stream stayed connected");
      location.reload();
    } catch (err) { toast(err.message, "err"); }
  });
}));
document.querySelectorAll(".delete-source").forEach(btn => btn.addEventListener("click", async () => {
  if (!(await confirmDialog("Delete this saved source?", { danger: true }))) return;
  try { await apiFetch(`/api/sources/${btn.dataset.id}`, { method: "DELETE" }); location.reload(); }
  catch (err) { toast(err.message, "err"); }
}));

// ---------------------------------------------------------------- Zoom account (Change 2)

document.getElementById("btn-zoom-google-signin").onclick = async (e) => {
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/zoom/google-signin", { method: "POST" });
      toast("Zoom launched for sign-in - open Remote GUI to complete it");
      document.getElementById("zoom-confirm-row").hidden = false;
    } catch (err) { toast(err.message, "err"); }
  });
};
document.getElementById("btn-zoom-confirm-signin").onclick = async () => {
  const label = document.getElementById("zoom-account-label").value.trim();
  if (!label) { toast("Enter a label first", "err"); return; }
  try {
    await apiFetch("/api/zoom/account", { method: "POST", body: JSON.stringify({ signed_in: true, label }) });
    toast("Marked as signed in"); location.reload();
  } catch (err) { toast(err.message, "err"); }
};
document.getElementById("btn-zoom-signout").onclick = async (e) => {
  if (!(await confirmDialog("Sign the bot out of Zoom? You'll need to sign in again for account-based joins.", { danger: true }))) return;
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/zoom/signout", { method: "POST" });
      toast("Signed out"); location.reload();
    } catch (err) { toast(err.message, "err"); }
  });
};

// ---------------------------------------------------------------- stream key / settings (unchanged)

document.getElementById("toggle-stream-key").onclick = (e) => {
  const input = document.getElementById("cfg-stream-key");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  e.currentTarget.innerHTML = icon(showing ? "eye" : "eye-off", "icon-sm");
};

document.getElementById("save-stream-key").onclick = async (e) => {
  const key = document.getElementById("cfg-stream-key").value;
  if (!key) { toast("Enter a new key first", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      const res = await apiFetch("/api/config", { method: "POST", body: JSON.stringify({ YT_STREAM_KEY: key }) });
      document.getElementById("cfg-stream-key").value = "";
      await afterSave(res);
    } catch (err) { toast(err.message, "err"); }
  });
};

initSegmented(document.getElementById("preset-group"), (name) => {
  const p = PRESETS[name];
  document.getElementById("cfg-res").value = p.RESOLUTION;
  document.getElementById("cfg-fps").value = p.FPS;
  document.getElementById("cfg-vbitrate").value = p.VIDEO_BITRATE;
  document.getElementById("cfg-vbitrate-range").value = p.VIDEO_BITRATE;
  document.getElementById("cfg-abitrate").value = p.AUDIO_BITRATE;
  document.getElementById("cfg-abitrate-range").value = p.AUDIO_BITRATE;
});
syncRangeNumber(document.getElementById("cfg-vbitrate-range"), document.getElementById("cfg-vbitrate"));
syncRangeNumber(document.getElementById("cfg-abitrate-range"), document.getElementById("cfg-abitrate"));

document.getElementById("save-stream-settings").onclick = async (e) => {
  await withLoading(e.currentTarget, async () => {
    try {
      const res = await apiFetch("/api/config", { method: "POST", body: JSON.stringify({
        RESOLUTION: document.getElementById("cfg-res").value.trim(),
        FPS: document.getElementById("cfg-fps").value,
        VIDEO_BITRATE: document.getElementById("cfg-vbitrate").value,
        AUDIO_BITRATE: document.getElementById("cfg-abitrate").value,
      })});
      await afterSave(res);
    } catch (err) { toast(err.message, "err"); }
  });
};

async function afterSave(res) {
  if (!res.units_to_restart || res.units_to_restart.length === 0) { toast("Saved (no restart needed)"); return; }
  const list = res.units_to_restart.join(", ");
  const go = await confirmDialog(`Saved. Restart now to apply? Units: ${list}`, { confirmText: "Restart now" });
  if (!go) { toast("Saved - remember to restart: " + list); return; }
  try {
    await apiFetch("/api/config/apply-restarts", { method: "POST", body: JSON.stringify({ units: res.units_to_restart }) });
    toast("Restart sent");
  } catch (err) { toast(err.message, "err"); }
}

document.getElementById("save-vnc-pass").onclick = async (e) => {
  const pw = document.getElementById("cfg-vnc-pass").value;
  if (pw.length < 4) { toast("Password too short", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/config/vnc-password", { method: "POST", body: JSON.stringify({ new_password: pw }) });
      document.getElementById("cfg-vnc-pass").value = "";
      toast("VNC password rotated and x11vnc restarted");
    } catch (err) { toast(err.message, "err"); }
  });
};

document.getElementById("save-settings").onclick = async (e) => {
  const body = { auto_recovery: document.getElementById("cfg-auto-recovery").checked };
  const webhook = document.getElementById("cfg-webhook").value;
  if (webhook) body.webhook_url = webhook;
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/settings", { method: "POST", body: JSON.stringify(body) });
      document.getElementById("cfg-webhook").value = "";
      toast("Settings saved");
    } catch (err) { toast(err.message, "err"); }
  });
};
