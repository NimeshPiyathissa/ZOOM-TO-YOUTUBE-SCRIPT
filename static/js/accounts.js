// Accounts page (Part 2): interactive Google sign-in over embedded noVNC.
// Nothing here ever sees a password/code/token - the sign-in happens
// inside the remote desktop; this page only opens/closes that window and
// asks the backend to verify with Google afterwards.
import { connectVnc } from '/static/js/vnc-embed.js';

const STATE_LABEL = { never: "Never signed in", signed_in: "Signed in", signed_out: "Signed out", inconclusive: "Couldn't verify" };

function fmtVerified(ts) {
  if (!ts) return "never verified";
  const ago = Math.max(0, (Date.now() / 1000) - Number(ts));
  return "verified " + (ago < 60 ? "just now" : ago < 3600 ? `${Math.floor(ago / 60)}m ago` : ago < 86400 ? `${Math.floor(ago / 3600)}h ago` : `${Math.floor(ago / 86400)}d ago`);
}

function paintCard(card, a) {
  if (a.label != null) { card.dataset.label = a.label; card.querySelector(".account-label").textContent = a.label; }
  if (a.state) {
    const badge = card.querySelector(".account-state");
    badge.dataset.state = a.state;
    badge.querySelector(".account-state-text").textContent = STATE_LABEL[a.state] || a.state;
    card.querySelector(".act-signin span").textContent = (a.state === "signed_out" || a.state === "inconclusive") ? "Re-authenticate" : "Sign in";
  }
  if (a.identity_masked !== undefined) card.querySelector(".account-identity").textContent = a.identity_masked || "identity unknown";
  if (a.last_verified_at !== undefined) { const el = card.querySelector(".account-verified"); el.dataset.ts = a.last_verified_at || ""; el.textContent = fmtVerified(a.last_verified_at); }
  if (a.last_result !== undefined) card.querySelector(".account-result").textContent = a.last_result || "";
}

document.querySelectorAll(".account-card").forEach((card) => {
  const badge = card.querySelector(".account-state");
  badge.querySelector(".account-state-text").textContent = STATE_LABEL[badge.dataset.state] || badge.dataset.state;
  const v = card.querySelector(".account-verified");
  v.textContent = fmtVerified(v.dataset.ts);
});

async function refreshCard(card) {
  const list = await apiFetch("/api/accounts");
  const a = list.find((x) => x.id === Number(card.dataset.accountId));
  if (a) paintCard(card, a);
}

// ---------------------------------------------------------------- add / rename / remove

document.getElementById("account-add").addEventListener("click", async (e) => {
  const input = document.getElementById("account-new-label");
  const label = input.value.trim();
  if (!label) { toast("Give the account a label first", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/accounts", { method: "POST", body: JSON.stringify({ label }) });
      toast("Account added - now use Sign in");
      location.reload();
    } catch (err) { toast(err.message, "err"); }
  });
});

document.getElementById("account-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-account-id]");
  if (!btn) return;
  const card = btn.closest(".account-card");
  const id = Number(btn.dataset.accountId);

  if (btn.classList.contains("act-rename")) {
    const label = prompt("New label", card.dataset.label || "");
    if (label == null) return;
    try { await apiFetch(`/api/accounts/${id}`, { method: "PUT", body: JSON.stringify({ label }) }); paintCard(card, { label: label.trim() }); toast("Renamed"); }
    catch (err) { toast(err.message, "err"); }
    return;
  }
  if (btn.classList.contains("act-remove")) {
    if (!(await confirmDialog(`Remove "${card.dataset.label}"? Its Chrome profile - and the Google session inside it - is deleted from the VPS.`, { danger: true, confirmText: "Remove" }))) return;
    await withLoading(btn, async () => {
      try { await apiFetch(`/api/accounts/${id}`, { method: "DELETE" }); card.remove(); toast("Account removed"); }
      catch (err) { toast(err.message, "err"); }
    });
    return;
  }
  if (btn.classList.contains("act-verify")) {
    await withLoading(btn, () => runVerify(card, id));
    return;
  }
  if (btn.classList.contains("act-signin")) {
    await withLoading(btn, () => startSignin(card, id));
  }
});

async function runVerify(card, id) {
  try {
    const r = await apiFetch(`/api/accounts/${id}/verify`, { method: "POST" });
    await refreshCard(card);
    const msg = r.state === "signed_in" ? `Signed in as ${r.identity_masked}` : r.state === "signed_out" ? "Google reports no active session" : `Couldn't verify: ${r.reason || "unknown"}`;
    toast(msg, r.state === "signed_in" ? "ok" : "err");
    announce(msg);
    return r;
  } catch (err) { toast(err.message, "err"); }
}

// ---------------------------------------------------------------- sign-in flow (embedded noVNC)

const panel = document.getElementById("signin-panel");
let signinCard = null, signinId = null, rfb = null, statusTimer = null;

async function startSignin(card, id) {
  try {
    await apiFetch(`/api/accounts/${id}/signin/start`, { method: "POST" });
  } catch (err) { toast(err.message, "err"); return; }
  signinCard = card; signinId = id;
  document.getElementById("signin-account-label").textContent = card.dataset.label;
  document.getElementById("signin-status").textContent = "Chrome is opening on the remote desktop…";
  panel.hidden = false;
  panel.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  const target = document.getElementById("signin-vnc");
  target.innerHTML = "";
  rfb = connectVnc(target);
  announce(`Sign-in window opened for ${card.dataset.label}`);
  clearInterval(statusTimer);
  statusTimer = setInterval(pollSigninStatus, 5000);
}

async function pollSigninStatus() {
  if (signinId == null) return;
  try {
    const s = await apiFetch(`/api/accounts/${signinId}/signin/status`);
    document.getElementById("signin-status").textContent = s.signin_open
      ? "Sign-in window is open on the remote desktop. Tap “I'm done” when you can see your account avatar."
      : "The sign-in window has closed. Tap “I'm done” to verify, or Sign in again to reopen it.";
  } catch (err) { /* transient */ }
}

function closePanel() {
  clearInterval(statusTimer); statusTimer = null;
  if (rfb) { try { rfb.disconnect(); } catch (e) { /* already gone */ } rfb = null; }
  panel.hidden = true;
  signinCard = null; signinId = null;
}

document.getElementById("signin-done").addEventListener("click", async (e) => {
  if (signinId == null) return;
  await withLoading(e.currentTarget, async () => {
    document.getElementById("signin-status").textContent = "Closing the window so the session is saved, then asking Google…";
    const r = await runVerify(signinCard, signinId);
    if (r && r.state === "signed_in") closePanel();
    else document.getElementById("signin-status").textContent = r
      ? (r.state === "signed_out"
          ? "Google still reports no session. Tap Sign in again to reopen the window and finish any remaining step."
          : `Couldn't confirm yet (${r.reason || "no answer"}). If you finished signing in, wait a moment and tap Verify on the account.`)
      : "Verification failed - see the message above.";
  });
});

document.getElementById("signin-cancel").addEventListener("click", async (e) => {
  if (signinId == null) { closePanel(); return; }
  await withLoading(e.currentTarget, async () => {
    try { await apiFetch(`/api/accounts/${signinId}/signin/cancel`, { method: "POST" }); } catch (err) { toast(err.message, "err"); }
    closePanel();
  });
});
