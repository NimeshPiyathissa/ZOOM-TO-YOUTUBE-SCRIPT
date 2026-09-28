// Accounts page: interactive Google sign-in over embedded noVNC.
// Nothing here ever sees a password/code/cookie/token - the sign-in
// happens inside the remote screen; this page only opens/closes that
// window and asks the backend to verify with Google afterwards. The
// "import" path copies the stream browser's existing session (as the
// streaming user, on the VPS) and then verifies it the same way.
import { connectVnc } from '/static/js/vnc-embed.js';

// Mirrors the BADGE_ICON/BADGE_TEXT maps in templates/accounts.html -
// keep both in sync when adding a state. badge_state is verify_status
// (never/verified/wrong_account/signed_out/check_failed) with one extra
// value, "stale", for a verified-but-overdue-for-recheck session - see
// accounts.py's _row_view for why that's collapsed into one field.
const BADGE_ICON = { never: "circle-help", verified: "circle-check", stale: "alert-triangle", wrong_account: "circle-slash", signed_out: "user-x", check_failed: "alert-circle", checking: "loader-circle" };
const BADGE_TEXT = { never: "Never verified", verified: "Verified", stale: "Needs re-authentication", wrong_account: "Wrong account", signed_out: "Signed out", check_failed: "Couldn't verify", checking: "Checking…" };
const $ = (id) => document.getElementById(id);
const post = (url, body) => apiFetch(url, { method: "POST", body: body ? JSON.stringify(body) : undefined });

function fmtVerified(ts) {
  if (!ts) return "never verified";
  const ago = Math.max(0, (Date.now() / 1000) - Number(ts));
  return "verified " + (ago < 60 ? "just now" : ago < 3600 ? `${Math.floor(ago / 60)} min ago` : ago < 86400 ? `${Math.floor(ago / 3600)} h ago` : `${Math.floor(ago / 86400)} d ago`);
}

function setBadge(card, badgeState) {
  const badge = card.querySelector(".account-state");
  badge.dataset.state = badgeState;
  badge.querySelector(".account-state-text").textContent = BADGE_TEXT[badgeState] || badgeState;
  badge.querySelector(".account-state-icon use").setAttribute("href", `#i-${BADGE_ICON[badgeState] || "circle-help"}`);
}

// The transient client-only "checking" state (never persisted - the
// server has no such status) while a Verify now click is in flight.
function setChecking(card, on) {
  if (on) { card.dataset.checking = "1"; setBadge(card, "checking"); }
  else delete card.dataset.checking;
}

function paintCard(card, a) {
  if (a.label != null) { card.dataset.label = a.label; card.querySelector(".account-label").textContent = a.label; }
  if (a.badge_state) {
    setBadge(card, a.badge_state);
    card.dataset.state = a.verify_status || a.badge_state;
    card.querySelector(".act-signin span").textContent = a.needs_reauth ? "Re-authenticate" : "Sign in";
  }
  const identityEl = card.querySelector(".account-identity");
  if (a.verify_status === "wrong_account" && a.identity_masked && a.expected_identity_masked) {
    identityEl.innerHTML = "";
    identityEl.dataset.wrong = "1";
    identityEl.append("Signed in as ");
    const gotEl = document.createElement("strong"); gotEl.textContent = a.identity_masked; identityEl.append(gotEl);
    identityEl.append(" - expected ");
    const wantEl = document.createElement("strong"); wantEl.textContent = a.expected_identity_masked; identityEl.append(wantEl);
  } else if (a.identity_masked !== undefined) {
    delete identityEl.dataset.wrong;
    identityEl.textContent = a.identity_masked || "identity unknown";
  }
  if (a.last_verified_at !== undefined) { const el = card.querySelector(".account-verified"); el.dataset.ts = a.last_verified_at || ""; el.textContent = fmtVerified(a.last_verified_at); }
  if (a.last_result !== undefined) card.querySelector(".account-result").textContent = a.last_result || "";
}

// Badge icon/text are already correct from the server render (Jinja
// uses the same BADGE_ICON/BADGE_TEXT maps) - only the relative
// "verified N ago" time needs JS, since it depends on the viewer's
// clock at load time, not the server's render time.
document.querySelectorAll(".account-verified").forEach((v) => { v.textContent = fmtVerified(v.dataset.ts); });

async function refreshCard(card) {
  const list = await apiFetch("/api/accounts");
  const a = list.find((x) => x.id === Number(card.dataset.accountId));
  if (a) paintCard(card, a);
}

// ---------------------------------------------------------------- add / import

$("account-add").addEventListener("click", async (e) => {
  const label = $("account-new-label").value.trim();
  if (!label) { toast("Give the account a label first", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try { await post("/api/accounts", { label }); toast("Account added - now use Sign in"); location.reload(); }
    catch (err) { toast(err.message, "err"); }
  });
});

$("account-import").addEventListener("click", async (e) => {
  const label = $("account-import-label").value.trim();
  if (!label) { toast("Give the imported account a label first", "err"); return; }
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await post("/api/accounts/import", { label });
      const v = r.verify || {};
      const msg = v.verify_status === "verified" ? `Imported - Google confirms ${v.identity_masked || "a session"}`
        : `Imported, but Google reports: ${v.verify_status === "signed_out" ? "no session in the stream profile" : (v.reason || "couldn't verify")}`;
      toast(msg, v.verify_status === "verified" ? "ok" : "err");
      setTimeout(() => location.reload(), 900);
    } catch (err) { toast(err.message, "err"); }
  });
});

// ---------------------------------------------------------------- per-account actions

$("account-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-account-id]");
  if (!btn) return;
  const card = btn.closest(".account-card");
  const id = Number(btn.dataset.accountId);
  const label = card.dataset.label;

  if (btn.classList.contains("act-rename")) {
    const v = prompt("New label", label || "");
    if (v == null) return;
    try { await apiFetch(`/api/accounts/${id}`, { method: "PUT", body: JSON.stringify({ label: v }) }); paintCard(card, { label: v.trim() }); toast("Renamed"); }
    catch (err) { toast(err.message, "err"); }
    return;
  }
  if (btn.classList.contains("act-remove")) {
    if (!(await confirmDialog(`Remove "${label}"? Its Chrome profile - and the Google session inside it - is deleted from the VPS, along with its backups. Meetings and sources bound to it fall back to guest / the shared profile.`, { danger: true, confirmText: "Remove" }))) return;
    await withLoading(btn, async () => {
      try { await apiFetch(`/api/accounts/${id}`, { method: "DELETE" }); card.remove(); toast("Account removed"); }
      catch (err) { toast(err.message, "err"); }
    });
    return;
  }
  if (btn.classList.contains("act-signout")) {
    if (!(await confirmDialog(`Sign "${label}" out? The profile is cleared (Google session gone); the account stays so you can sign in again. Back it up first if you may want it back.`, { danger: true, confirmText: "Sign out" }))) return;
    await withLoading(btn, async () => {
      try { await post(`/api/accounts/${id}/signout`); await refreshCard(card); toast("Signed out - profile cleared"); }
      catch (err) { toast(err.message, "err"); }
    });
    return;
  }
  if (btn.classList.contains("act-backup")) {
    await withLoading(btn, async () => {
      try { const r = await post(`/api/accounts/${id}/backup`); toast(`Backed up the session (${Math.round(r.size / 1024)} KB, kept on the VPS outside git; newest 3 per account)`); }
      catch (err) { toast(err.message, "err"); }
    });
    return;
  }
  if (btn.classList.contains("act-restore")) {
    let backups = [];
    try { backups = await apiFetch(`/api/accounts/${id}/backups`); } catch (err) { toast(err.message, "err"); return; }
    if (!backups.length) { toast("No backup for this account yet", "err"); return; }
    const newest = backups[0];
    if (!(await confirmDialog(`Restore "${label}" from ${new Date(newest.ts * 1000).toLocaleString()} (${Math.round(newest.size / 1024)} KB)? The current profile is replaced, then verified with Google.`, { confirmText: "Restore" }))) return;
    await withLoading(btn, async () => {
      try {
        const r = await post(`/api/accounts/${id}/restore`, { name: newest.name });
        await refreshCard(card);
        const msg = r.verify_status === "verified" ? `Restored - Google confirms ${r.identity_masked || "a session"}`
          : r.verify_status === "wrong_account" ? `Restored, but that backup signs in as ${r.identity_masked}, not the expected ${r.expected_identity_masked}`
          : `Restored, but Google reports ${r.verify_status === "signed_out" ? "no session" : (r.reason || "couldn't verify")}`;
        toast(msg, r.verify_status === "verified" ? "ok" : "err");
      } catch (err) { toast(err.message, "err"); }
    });
    return;
  }
  if (btn.classList.contains("act-verify")) { await withLoading(btn, () => runVerify(card, id)); return; }
  if (btn.classList.contains("act-signin")) { await withLoading(btn, () => startSignin(card, id)); }
});

async function runVerify(card, id) {
  setChecking(card, true);
  try {
    const r = await post(`/api/accounts/${id}/verify`);
    await refreshCard(card);
    const msg = r.verify_status === "verified" ? `Verified - signed in as ${r.identity_masked || "(email not shown)"}`
      : r.verify_status === "wrong_account" ? `Wrong account: signed in as ${r.identity_masked}, expected ${r.expected_identity_masked}`
      : r.verify_status === "signed_out" ? "Google reports no active session - use Re-authenticate"
      : `Couldn't verify: ${r.reason || "unknown"}`;
    toast(msg, r.verify_status === "verified" ? "ok" : "err");
    announce(msg);
    return r;
  } catch (err) { toast(err.message, "err"); return null; }
  finally { setChecking(card, false); }
}

// ---------------------------------------------------------------- sign-in flow (embedded noVNC)

const panel = $("signin-panel");
let signinCard = null, signinId = null, rfb = null, statusTimer = null;

async function startSignin(card, id) {
  try { await post(`/api/accounts/${id}/signin/start`); }
  catch (err) { toast(err.message, "err"); return; }
  signinCard = card; signinId = id;
  $("signin-account-label").textContent = card.dataset.label;
  $("signin-status").textContent = "Chrome is opening on the remote screen…";
  panel.hidden = false;
  panel.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  const target = $("signin-vnc");
  target.innerHTML = "";
  rfb = connectVnc(target, { onDisconnect: () => { rfb = null; } });
  post("/api/vnc/rate", { mode: "fast" }).catch(() => {});
  announce(`Sign-in window opened for ${card.dataset.label}`);
  clearInterval(statusTimer);
  statusTimer = setInterval(pollSigninStatus, 5000);
}

async function pollSigninStatus() {
  if (signinId == null) return;
  try {
    const s = await apiFetch(`/api/accounts/${signinId}/signin/status`);
    $("signin-status").textContent = s.signin_open
      ? "Sign-in window is open on the remote screen. Tap “I'm done” when you can see your account avatar."
      : "The sign-in window has closed. Tap “I'm done” to verify, or Sign in again to reopen it.";
  } catch (err) { /* transient */ }
}

function closePanel() {
  clearInterval(statusTimer); statusTimer = null;
  if (rfb) { try { rfb.disconnect(); } catch (e) { /* already gone */ } rfb = null; }
  post("/api/vnc/rate", { mode: "slow" }).catch(() => {});
  panel.hidden = true;
  signinCard = null; signinId = null;
}

$("signin-done").addEventListener("click", async (e) => {
  if (signinId == null) return;
  await withLoading(e.currentTarget, async () => {
    $("signin-status").textContent = "Closing the window so the session is written to disk, then asking Google…";
    const r = await runVerify(signinCard, signinId);
    // Only a genuine "verified" auto-closes the panel - wrong_account
    // still reports Google's session as real (state === "signed_in"),
    // it's just not the identity this account expects, so the operator
    // needs to see that and decide (sign out and redo, or accept it as
    // the new expected identity by signing out first).
    if (r && r.verify_status === "verified") closePanel();
    else $("signin-status").textContent = r
      ? (r.verify_status === "wrong_account"
          ? `Signed in, but as ${r.identity_masked} - this account expects ${r.expected_identity_masked}. Sign out first if you meant to switch identities, then redo sign-in.`
          : r.verify_status === "signed_out"
          ? "Google still reports no session. Tap Sign in again to reopen the window and finish any remaining step (2-Step prompt, device confirmation)."
          : `Couldn't confirm yet (${r.reason || "no answer"}). If you finished signing in, wait a moment and tap Verify now on the account.`)
      : "Verification failed - see the message above.";
  });
});

$("signin-cancel").addEventListener("click", async (e) => {
  if (signinId == null) { closePanel(); return; }
  await withLoading(e.currentTarget, async () => {
    try { await post(`/api/accounts/${signinId}/signin/cancel`); } catch (err) { toast(err.message, "err"); }
    closePanel();
  });
});
window.addEventListener("pagehide", () => { if (rfb) fetch("/api/vnc/rate", { method: "POST", credentials: "same-origin", keepalive: true, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() }, body: JSON.stringify({ mode: "slow" }) }).catch(() => {}); });

// ---------------------------------------------------------------- Zoom client (its own session)

async function zoomAccount() {
  let z; try { z = await apiFetch("/api/zoom/account"); } catch (err) { return; }
  const badge = $("ac-zoom-badge"), text = $("ac-zoom-text"), detail = $("ac-zoom-detail");
  if (z.confirmed_signed_in) { badge.className = "badge badge-active"; text.textContent = "Signed in" + (z.label ? " · " + z.label : ""); }
  else { badge.className = "badge badge-inactive"; text.textContent = "Not confirmed"; }
  detail.textContent = z.heuristic && z.heuristic.session_files_present ? "Zoom's data directory has session files (not authoritative)." : "No Zoom session files found.";
}
$("ac-zoom-signin").addEventListener("click", (e) => withLoading(e.currentTarget, async () => {
  if (!(await confirmDialog("Open the Zoom client at its sign-in screen on the remote screen? Zoom must not be in a meeting. Use its Sign In button, then Google - the browser that opens is the Chrome profile of the account bound to the active source (or the shared profile).", { confirmText: "Open Zoom" }))) return;
  try { await post("/api/zoom/google-signin"); toast("Zoom opened on the remote screen - complete the sign-in there, then tap “I signed in”"); }
  catch (err) { toast(err.message, "err"); }
}));
$("ac-zoom-confirm").addEventListener("click", (e) => withLoading(e.currentTarget, async () => {
  const label = prompt("Which account did you sign Zoom in with? (label only, e.g. Main channel)", "");
  if (label == null) return;
  try { await post("/api/zoom/account", { signed_in: true, label }); await zoomAccount(); toast("Recorded"); } catch (err) { toast(err.message, "err"); }
}));
$("ac-zoom-signout").addEventListener("click", (e) => withLoading(e.currentTarget, async () => {
  if (!(await confirmDialog("Ask the Zoom client to forget its session?", { danger: true, confirmText: "Sign out" }))) return;
  try { await post("/api/zoom/signout"); await post("/api/zoom/account", { signed_in: false }); await zoomAccount(); toast("Zoom signed out"); } catch (err) { toast(err.message, "err"); }
}));
zoomAccount();
