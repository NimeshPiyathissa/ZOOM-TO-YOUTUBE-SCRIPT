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
    return {
        "id": r["id"],
        "label": r["label"],
        "profile_id": r["profile_id"],
        "identity_masked": mask_email(r["email"]),
        "state": r["state"],
        "last_verified_at": r["last_verified_at"],
        "last_result": r["last_result"],
        "created_at": r["created_at"],
    }


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
    status = result.get("status", "inconclusive")
    if status not in ("signed_in", "signed_out", "inconclusive"):
        status = "inconclusive"
    email = result.get("email") if status == "signed_in" else None
    reason = result.get("reason", "")
    now = time.time()
    with db.get_conn() as conn:
        if status == "signed_in":
            conn.execute(
                "UPDATE accounts SET state=?, email=?, last_verified_at=?, last_result=? WHERE id=?",
                (status, email, now, "Google confirmed an active session", account_id),
            )
        else:
            # Keep the previously-known email (still useful as a label)
            # but the state is now whatever Google actually said.
            conn.execute(
                "UPDATE accounts SET state=?, last_verified_at=?, last_result=? WHERE id=?",
                (status, now, reason or ("No active Google session" if status == "signed_out" else ""), account_id),
            )
    return {"state": status, "identity_masked": mask_email(email) if email else None, "reason": reason}
