"""Google accounts for interactive sign-in (Part 2).

What this stores: a label, the id of the zoombot-owned Chrome profile
directory that backs the account, the email Google reported the last
time we verified, the email an account is *expected* to hold (pinned
from its own first successful verify - see verify_account()), and that
verification result. What this NEVER stores, reads, or transmits:
passwords, 2FA codes, OAuth tokens, cookies. Sign-in is done by a human
over noVNC in an ordinary Chrome window; "verify" asks Google (with the
profile's own cookies, inside zoombot's session) whether a session
exists and records only the answer.

Why session verification, not OAuth: an OAuth flow run in an admin's own
browser proves they own a Google account - it says nothing about
whether *this specific Chrome profile on the VPS* (the one that
actually plays age-restricted YouTube videos and backs Zoom's "Sign in
with Google") has a live session. An OAuth-based badge could show green
while the VPS profile is signed out - a false green, discovered only
when a stream fails. See Part 4 (a separate, genuinely OAuth-shaped
feature: the YouTube Data API) for where an application token actually
belongs.

verify_status (the badge-facing state, derived - never stored directly)
is one of:
  never          - no verification has ever succeeded
  verified       - Google confirms an active session for the expected identity
  wrong_account  - Google confirms an active session, but for a different email
  signed_out     - Google redirected to its own sign-in page: no session
  check_failed   - the check itself didn't reach a conclusion (network
                   timeout, an unexpected page, a security challenge,
                   the profile busy in another Chrome right now, etc.)
This is intentionally never collapsed to a binary - "verified" and
"the check failed to tell us anything" are very different situations
for an operator about to go live.

Everything that touches the profile directory runs as zoombot through
scripts/chrome-account.sh (control.account_profile_action); the
dashboard user has no read access to those directories at all."""
from __future__ import annotations

import json
import re
import secrets
import time

from . import control, db


class AccountError(Exception):
    pass


_LABEL_RE = re.compile(r"^[^\r\n]{1,60}$")

# A verified session older than this reads as stale (amber, "needs
# re-authentication") even though the last check actually succeeded -
# twice the scheduled background-verify interval (scheduler.py), so one
# missed run doesn't immediately flip the badge, but two does.
STALE_AFTER_SECONDS = 12 * 3600


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return f"{local[0]}•••@{domain}"
    return f"{local[0]}•••{local[-1]}@{domain}"


def _verify_status(state: str, email: str | None, expected_email: str | None) -> str:
    if state == "signed_in":
        if email and expected_email and email != expected_email:
            return "wrong_account"
        return "verified"
    if state == "signed_out":
        return "signed_out"
    if state == "inconclusive":
        return "check_failed"
    return "never"


def _row_view(r) -> dict:
    email = r["email"]
    expected_email = r["expected_email"]
    verify_status = _verify_status(r["state"], email, expected_email)
    last_verified_at = r["last_verified_at"]
    is_stale = bool(last_verified_at) and (time.time() - last_verified_at) > STALE_AFTER_SECONDS
    # badge_state: verify_status, except a verified-but-stale session
    # gets its own value - the one thing the template/JS actually key
    # their icon/colour off, so "why is this amber" always has exactly
    # one answer instead of two fields to cross-reference.
    badge_state = "stale" if (verify_status == "verified" and is_stale) else verify_status
    return {
        "id": r["id"],
        "label": r["label"],
        "profile_id": r["profile_id"],
        "identity_masked": mask_email(email),
        "expected_identity_masked": mask_email(expected_email),
        "state": r["state"],
        "verify_status": verify_status,
        "badge_state": badge_state,
        # verified: safe to select as a source's playback identity right
        # now. Deliberately NOT staleness-gated - a stale-but-verified
        # session is still the last known-good answer, just due for a
        # recheck; only an outcome that actively contradicts it
        # (wrong_account/signed_out/check_failed) should block use.
        "verified": verify_status == "verified",
        "is_stale": is_stale,
        # needs_reauth: anything that should show the amber/red
        # "re-authenticate before it bites mid-stream" treatment.
        "needs_reauth": verify_status in ("wrong_account", "signed_out", "check_failed") or (verify_status == "verified" and is_stale),
        "last_verified_at": last_verified_at,
        "last_result": r["last_result"],
        "created_at": r["created_at"],
    }


def _find_by_profile(profile_id: str) -> dict | None:
    with db.get_conn() as conn:
        r = conn.execute("SELECT * FROM accounts WHERE profile_id=?", (profile_id,)).fetchone()
        return _row_view(r) if r else None


def list_accounts() -> list[dict]:
    with db.get_conn() as conn:
        return [_row_view(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()]


def get_account(account_id: int) -> dict | None:
    with db.get_conn() as conn:
        r = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return _row_view(r) if r else None


def _validate_label(label: str) -> str:
    label = (label or "").strip()
    if not _LABEL_RE.match(label):
        raise AccountError("Label must be 1-60 characters")
    return label


def create_account(label: str) -> int:
    label = _validate_label(label)
    profile_id = "acct-" + secrets.token_hex(4)
    control.account_profile_action("create", profile_id)
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO accounts (label, profile_id, state, created_at) VALUES (?,?,?,?)",
                (label, profile_id, "never", time.time()),
            )
            return cur.lastrowid
    except db.sqlite3.IntegrityError:
        control.account_profile_action("remove", profile_id)
        raise AccountError("An account with that label already exists")


def rename_account(account_id: int, label: str) -> None:
    label = _validate_label(label)
    with db.get_conn() as conn:
        try:
            conn.execute("UPDATE accounts SET label=? WHERE id=?", (label, account_id))
        except db.sqlite3.IntegrityError:
            raise AccountError("An account with that label already exists")


def remove_account(account_id: int) -> None:
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    control.account_profile_action("remove", acct["profile_id"])
    with db.get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))


def import_stream_profile(label: str) -> int:
    """A new account whose profile is a copy of the shared stream
    profile's session - the one Zoom's Google sign-in and the kiosk have
    been using (chrome-account.sh import: Local State + Default minus
    caches; no cookie is read or shown here). Verified right after."""
    label = _validate_label(label)
    profile_id = "acct-" + secrets.token_hex(4)
    control.account_profile_action("create", profile_id)
    try:
        control.account_profile_action("import", profile_id, timeout=60)
    except control.ControlError:
        control.account_profile_action("remove", profile_id)
        raise
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO accounts (label, profile_id, state, created_at, last_result) VALUES (?,?,?,?,?)",
                (label, profile_id, "never", time.time(), "Imported from the stream profile - verifying"),
            )
            aid = cur.lastrowid
    except db.sqlite3.IntegrityError:
        control.account_profile_action("remove", profile_id)
        raise AccountError("An account with that label already exists")
    return aid


def sign_out(account_id: int) -> None:
    """Forget the session: the profile becomes fresh and empty. The label
    and last-known (masked) email stay so the card can say who it was.
    expected_email is cleared, not kept: a deliberate sign-out is "start
    over" for this slot, so whichever Google account signs in next -
    even a different one - gets treated as correct and re-pinned on its
    own first successful verify, rather than immediately reading as
    wrong_account against the identity that was just signed out of."""
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    control.account_profile_action("signout", acct["profile_id"])
    with db.get_conn() as conn:
        conn.execute("UPDATE accounts SET state=?, expected_email=NULL, last_verified_at=?, last_result=? WHERE id=?",
                     ("signed_out", time.time(), "Signed out - the profile was cleared", account_id))


def backup_account(account_id: int) -> dict:
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    out = control.account_profile_action("backup", acct["profile_id"], timeout=120)
    kv = dict(part.split("=", 1) for part in out.split() if "=" in part)
    return {"backup": kv.get("backup", ""), "size": int(kv.get("size", "0") or 0)}


def list_backups(account_id: int) -> list[dict]:
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    out = control.account_profile_action("backups", acct["profile_id"])
    items = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            items.append({"name": parts[0], "size": int(parts[1]), "ts": int(parts[2])})
    return items


def restore_account(account_id: int, name: str | None = None) -> dict:
    """Restore the newest (or a named) backup of this account, then
    verify so the card shows what Google actually says."""
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    out = control.account_profile_action("restore", acct["profile_id"], timeout=120, extra=(name or None))
    result = verify_account(account_id)
    result["restored"] = out.partition("=")[2]
    return result


def signin_start(account_id: int) -> dict:
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    out = control.account_profile_action("signin", acct["profile_id"])
    return {"opened": out in ("opened", "already-open")}


def signin_cancel(account_id: int) -> None:
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    control.account_profile_action("close", acct["profile_id"])


def signin_status(account_id: int) -> dict:
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    out = control.account_profile_action("status", acct["profile_id"], timeout=10)
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"signin_open": kv.get("signin_open") == "yes", "in_use": kv.get("in_use") == "yes"}


def _verify_via_running_kiosk(profile_id: str) -> dict | None:
    import asyncio
    from . import cdp
    try:
        cur = control.read_current_source()
    except control.ControlError:
        return None
    if cur.get("SOURCE_TYPE") != "webpage" or cur.get("ACCOUNT_PROFILE_ID") != profile_id:
        return None
    try:
        res = asyncio.run(cdp.evaluate(cdp.JS_GOOGLE_IDENTITY))
    except Exception:  # noqa: BLE001 - CDPError or a closed loop; caller keeps the script's answer
        return None
    val = res.get("value")
    if val:
        return {"status": "signed_in", "email": val, "via": "running browser"}
    if val == "":
        return {"status": "signed_in", "via": "running browser (email not shown on this page)"}
    return {"status": "inconclusive", "reason": "profile is in use by the running web source and its page shows no Google account button - check again when it is on YouTube/Google"}


def verify_all(reason: str = "scheduled") -> list[dict]:
    """Background pass over every account that has ever had a session.
    Skips one whose sign-in window is open. Flags a lost session in the
    audit log so the operator sees it before a stream needs it - keyed
    off verify_status, not the raw signed_in/signed_out state, because a
    wrong_account result still reports state=signed_in (Google really
    does have an active session - just not the right one) and would
    otherwise never trigger this alert."""
    out = []
    for a in list_accounts():
        if a["state"] == "never" and not a["identity_masked"]:
            continue
        try:
            st = signin_status(a["id"])
            if st.get("signin_open"):
                continue
            before = a["verify_status"]
            r = verify_account(a["id"])
            out.append({"id": a["id"], "label": a["label"], "state": r["state"], "verify_status": r["verify_status"]})
            if before == "verified" and r["verify_status"] != "verified":
                db.audit(None, "account_session_lost", f"{a['id']}:{a['label']} -> {r['verify_status']} ({reason})")
        except (AccountError, control.ControlError) as exc:
            out.append({"id": a["id"], "label": a["label"], "error": str(exc)[:120]})
    return out


def verify_account(account_id: int) -> dict:
    """Closes any open sign-in window first (so cookies are flushed),
    then asks Google. Records exactly what came back, and - on the
    first ever signed-in result for this account - pins that identity
    as expected_email so a later session for a *different* Google
    account reads as wrong_account instead of silently overwriting it."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise AccountError("account not found")
    acct = _row_view(row)
    control.account_profile_action("close", acct["profile_id"])
    raw = control.account_profile_action("verify", acct["profile_id"], timeout=60)
    try:
        result = json.loads(raw.splitlines()[-1]) if raw else {}
    except (json.JSONDecodeError, IndexError):
        result = {}
    if result.get("in_use"):
        # The running kiosk holds this profile (bound web source): ask
        # that Chrome over DevTools which Google account its page shows
        # instead of fighting it for the directory.
        kiosk = _verify_via_running_kiosk(acct["profile_id"])
        if kiosk:
            result = kiosk
        elif row["state"] == "signed_in":
            # Profile is actively in use by a running source (e.g. Zoom,
            # YouTube kiosk). Do not demote an active session to
            # inconclusive while in use - re-assert the raw stored email
            # (not the masked view) so the wrong_account comparison
            # below still has something real to compare.
            result = {
                "status": "signed_in",
                "email": row["email"],
                "via": "running source (profile in use)",
            }
    status = result.get("status", "inconclusive")
    if status not in ("signed_in", "signed_out", "inconclusive"):
        status = "inconclusive"
    email = result.get("email") if status == "signed_in" else None
    reason = result.get("reason", "")
    now = time.time()
    expected_email = row["expected_email"]
    verify_status = _verify_status(status, email, expected_email)
    with db.get_conn() as conn:
        if status == "signed_in":
            # First-ever signed-in result for this account pins the
            # identity; afterwards expected_email only ever changes via
            # an explicit sign_out() (a deliberate "start over").
            new_expected = expected_email or email
            if verify_status == "wrong_account":
                msg = "Signed in as a different account than expected (see the card for both)"
            elif result.get("via"):
                msg = "Google confirmed an active session via the " + result["via"]
            else:
                msg = "Google confirmed an active session"
            if email:
                conn.execute(
                    "UPDATE accounts SET state=?, email=?, expected_email=?, last_verified_at=?, last_result=? WHERE id=?",
                    (status, email, new_expected, now, msg, account_id),
                )
            else:
                conn.execute(
                    "UPDATE accounts SET state=?, last_verified_at=?, last_result=? WHERE id=?",
                    (status, now, "Session confirmed via the " + (result.get("via") or "browser"), account_id),
                )
        else:
            # Keep the previously-known email/expected_email (still
            # useful as labels) - only the state changes to whatever
            # Google actually said.
            conn.execute(
                "UPDATE accounts SET state=?, last_verified_at=?, last_result=? WHERE id=?",
                (status, now, reason or ("No active Google session" if status == "signed_out" else ""), account_id),
            )
    final = get_account(account_id)
    return {
        "state": status,
        "verify_status": final["verify_status"],
        "badge_state": final["badge_state"],
        "identity_masked": final["identity_masked"],
        "expected_identity_masked": final["expected_identity_masked"],
        "reason": reason,
    }
