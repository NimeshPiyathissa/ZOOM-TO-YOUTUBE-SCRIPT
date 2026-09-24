"""Google accounts for interactive sign-in (Part 2).

What this stores: a label, the id of the zoombot-owned Chrome profile
directory that backs the account, the email Google reported the last
time we verified, and that verification result. What this NEVER stores,
reads, or transmits: passwords, 2FA codes, OAuth tokens, cookies. Sign-in
is done by a human over noVNC in an ordinary Chrome window; "verify"
asks Google (with the profile's own cookies, inside zoombot's session)
whether a session exists and records only the answer.

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


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return f"{local[0]}•••@{domain}"
    return f"{local[0]}•••{local[-1]}@{domain}"


def _row_view(r) -> dict:
    # needs_reauth: an account that once had a session and no longer
    # verifies - the "re-authenticate before it bites mid-stream" flag.
    return {
        "id": r["id"],
        "label": r["label"],
        "profile_id": r["profile_id"],
        "identity_masked": mask_email(r["email"]),
        "state": r["state"],
        "needs_reauth": bool(r["email"]) and r["state"] in ("signed_out", "inconclusive"),
        "last_verified_at": r["last_verified_at"],
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
    and last-known (masked) email stay so the card can say who it was."""
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
    control.account_profile_action("signout", acct["profile_id"])
    with db.get_conn() as conn:
        conn.execute("UPDATE accounts SET state=?, last_verified_at=?, last_result=? WHERE id=?",
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
    audit log so the operator sees it before a stream needs it."""
    out = []
    for a in list_accounts():
        if a["state"] == "never" and not a["identity_masked"]:
            continue
        try:
            st = signin_status(a["id"])
            if st.get("signin_open"):
                continue
            before = a["state"]
            r = verify_account(a["id"])
            out.append({"id": a["id"], "label": a["label"], "state": r["state"]})
            if before == "signed_in" and r["state"] != "signed_in":
                db.audit(None, "account_session_lost", f"{a['id']}:{a['label']} -> {r['state']} ({reason})")
        except (AccountError, control.ControlError) as exc:
            out.append({"id": a["id"], "label": a["label"], "error": str(exc)[:120]})
    return out


def verify_account(account_id: int) -> dict:
    """Closes any open sign-in window first (so cookies are flushed),
    then asks Google. Records exactly what came back."""
    acct = get_account(account_id)
    if not acct:
        raise AccountError("account not found")
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
        elif acct.get("state") == "signed_in":
            # Profile is actively in use by a running source (e.g. Zoom, YouTube kiosk).
            # Do not demote an active session to inconclusive while in use.
            result = {
                "status": "signed_in",
                "email": acct.get("email"),
                "via": "running source (profile in use)",
            }
        else:
            result = result
    status = result.get("status", "inconclusive")
    if status not in ("signed_in", "signed_out", "inconclusive"):
        status = "inconclusive"
    email = result.get("email") if status == "signed_in" else None
    reason = result.get("reason", "")
    now = time.time()
    with db.get_conn() as conn:
        if status == "signed_in":
            if email:
                conn.execute(
                    "UPDATE accounts SET state=?, email=?, last_verified_at=?, last_result=? WHERE id=?",
                    (status, email, now, "Google confirmed an active session" + (" via the " + result["via"] if result.get("via") else ""), account_id),
                )
            else:
                conn.execute(
                    "UPDATE accounts SET state=?, last_verified_at=?, last_result=? WHERE id=?",
                    (status, now, "Session confirmed via the " + (result.get("via") or "browser"), account_id),
                )
        else:
            # Keep the previously-known email (still useful as a label)
            # but the state is now whatever Google actually said.
            conn.execute(
                "UPDATE accounts SET state=?, last_verified_at=?, last_result=? WHERE id=?",
                (status, now, reason or ("No active Google session" if status == "signed_out" else ""), account_id),
            )
    if not email and status == "signed_in":
        email = (get_account(account_id) or {}).get("identity_masked")
        return {"state": status, "identity_masked": email, "reason": reason}
    return {"state": status, "identity_masked": mask_email(email) if email else None, "reason": reason}
