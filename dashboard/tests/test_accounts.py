"""Tests for the Accounts page's verification state machine
(app/accounts.py). No test here ever calls a real chrome-account.sh /
sudo / Chrome subprocess - control.account_profile_action is monkeypatched
to return a scripted string, exactly like the shell script's own stdout,
so these exercise the exact same JSON-parsing and state-derivation logic
that runs against the real script's output on the VPS.

The central thing under test is the claim in accounts.py's own docstring
and this project's design choice: verification asks Google, through the
VPS profile's own cookies, whether a session exists - never OAuth, never
a password. test_verify_now_after_signout_reports_signed_out_not_verified
is the literal "prove the false-green case is impossible" check: sign
the VPS profile out, run Verify Now, and the badge must say signed_out,
never verified.
"""
import json

import pytest

from app import accounts, config, control, db


@pytest.fixture(autouse=True)
def _no_real_zoombot_calls(monkeypatch):
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    # Safe default so create_account()'s own "create" call - which every
    # test below makes before it gets a chance to install its own
    # _mock_profile_action script - never falls through to a real
    # sudo/chrome-account.sh subprocess. Individual tests override this
    # via _mock_profile_action for the actions they actually care about.
    monkeypatch.setattr(control, "account_profile_action", lambda action, profile_id, timeout=20, extra=None: "")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield


def _mock_profile_action(monkeypatch, script):
    """script: dict mapping action -> return value (a string, as the
    real chrome-account.sh would print) or a callable(action, profile_id,
    **kwargs) -> str, for actions that need to vary per call."""
    def fake(action, profile_id, timeout=20, extra=None):
        if callable(script):
            return script(action, profile_id, timeout=timeout, extra=extra)
        if action not in script:
            return ""
        val = script[action]
        return val(profile_id) if callable(val) else val
    monkeypatch.setattr(control, "account_profile_action", fake)


def _signed_in(email: str) -> str:
    return json.dumps({"status": "signed_in", "email": email})


_SIGNED_OUT = json.dumps({"status": "signed_out"})
_CHECK_FAILED = json.dumps({"status": "inconclusive", "reason": "no page returned (network/timeout)"})


# ---------------------------------------------------------------- mask_email

def test_mask_email_short_local_part():
    assert accounts.mask_email("ab@gmail.com") == "a•••@gmail.com"


def test_mask_email_normal():
    assert accounts.mask_email("bandit@gmail.com") == "b•••t@gmail.com"


def test_mask_email_none_and_no_at():
    assert accounts.mask_email(None) is None
    assert accounts.mask_email("not-an-email") is None


# ---------------------------------------------------------------- _verify_status (pure state machine)

@pytest.mark.parametrize("state,email,expected_email,want", [
    ("signed_in", "a@gmail.com", None, "verified"),               # first-ever result: nothing pinned yet
    ("signed_in", "a@gmail.com", "a@gmail.com", "verified"),      # matches the pinned identity
    ("signed_in", "b@gmail.com", "a@gmail.com", "wrong_account"), # signed in, but not who was pinned
    ("signed_in", None, "a@gmail.com", "verified"),               # email not shown this time - best effort
    ("signed_out", None, "a@gmail.com", "signed_out"),
    ("inconclusive", None, "a@gmail.com", "check_failed"),
    ("never", None, None, "never"),
])
def test_verify_status_matrix(state, email, expected_email, want):
    assert accounts._verify_status(state, email, expected_email) == want


# ---------------------------------------------------------------- verify_account: pins on first success

def test_first_verify_pins_expected_email(tmp_db, monkeypatch):
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    r = accounts.verify_account(aid)
    assert r["verify_status"] == "verified"
    assert r["identity_masked"] == accounts.mask_email("main@gmail.com")
    a = accounts.get_account(aid)
    assert a["verify_status"] == "verified"
    assert a["needs_reauth"] is False


def test_second_verify_same_identity_stays_verified(tmp_db, monkeypatch):
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_account(aid)
    r = accounts.verify_account(aid)
    assert r["verify_status"] == "verified"


def test_different_identity_reports_wrong_account_not_verified(tmp_db, monkeypatch):
    """The core Part 1 requirement: a profile that now answers as a
    *different* Google account must never read as verified."""
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_account(aid)  # pins main@gmail.com

    _mock_profile_action(monkeypatch, {"verify": _signed_in("someone.else@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    r = accounts.verify_account(aid)
    assert r["verify_status"] == "wrong_account"
    assert r["identity_masked"] == accounts.mask_email("someone.else@gmail.com")
    assert r["expected_identity_masked"] == accounts.mask_email("main@gmail.com")
    a = accounts.get_account(aid)
    assert a["needs_reauth"] is True
    assert a["verified"] is False  # must not be selectable as a good source identity


def test_verify_now_after_signout_reports_signed_out_not_verified(tmp_db, monkeypatch):
    """The literal "prove the false-green case is impossible" check from
    the task: sign the VPS profile out, run Verify Now, confirm the
    result is signed_out - never verified, regardless of what an
    OAuth-based check running in an unrelated browser might claim."""
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no", "signout": "signed-out"})
    accounts.verify_account(aid)
    assert accounts.get_account(aid)["verify_status"] == "verified"

    accounts.sign_out(aid)
    _mock_profile_action(monkeypatch, {"verify": _SIGNED_OUT, "close": "closed", "status": "signin_open=no\nin_use=no"})
    r = accounts.verify_account(aid)
    assert r["verify_status"] == "signed_out"
    a = accounts.get_account(aid)
    assert a["verify_status"] == "signed_out"
    assert a["verified"] is False
    # expected_email was cleared by sign_out(), so a fresh sign-in to
    # *any* account (even a different one) re-pins cleanly instead of
    # immediately reading as wrong_account against the old identity.
    assert a["expected_identity_masked"] is None


def test_check_failed_is_distinct_from_signed_out(tmp_db, monkeypatch):
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _CHECK_FAILED, "close": "closed", "status": "signin_open=no\nin_use=no"})
    r = accounts.verify_account(aid)
    assert r["verify_status"] == "check_failed"
    assert r["verify_status"] != "signed_out"
    assert accounts.get_account(aid)["needs_reauth"] is True


def test_never_verified_account_has_grey_never_state(tmp_db, monkeypatch):
    aid = accounts.create_account("Fresh")
    a = accounts.get_account(aid)
    assert a["verify_status"] == "never"
    assert a["badge_state"] == "never"
    assert a["needs_reauth"] is False  # nothing to re-authenticate yet - it was simply never tried


def test_stale_verified_account_is_still_verified_but_flagged(tmp_db, monkeypatch):
    import time
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_account(aid)
    with db.get_conn() as conn:
        conn.execute("UPDATE accounts SET last_verified_at=? WHERE id=?", (time.time() - accounts.STALE_AFTER_SECONDS - 60, aid))
    a = accounts.get_account(aid)
    assert a["verify_status"] == "verified"     # last known answer is still "verified"...
    assert a["is_stale"] is True
    assert a["badge_state"] == "stale"          # ...but the badge says so, amber not green
    assert a["needs_reauth"] is True
    assert a["verified"] is True                # ...and it's still usable as a source identity


# ---------------------------------------------------------------- sign_out clears the pin

def test_sign_out_clears_expected_email(tmp_db, monkeypatch):
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no", "signout": "signed-out"})
    accounts.verify_account(aid)
    assert accounts.get_account(aid)["expected_identity_masked"] is not None
    accounts.sign_out(aid)
    assert accounts.get_account(aid)["expected_identity_masked"] is None


# ---------------------------------------------------------------- verify_all: audit trail on a lost session

def test_verify_all_audits_verified_to_wrong_account_transition(tmp_db, monkeypatch):
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_account(aid)

    audited = []
    monkeypatch.setattr(db, "audit", lambda username, action, detail="": audited.append((action, detail)))
    _mock_profile_action(monkeypatch, {"verify": _signed_in("someone.else@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_all(reason="test")
    assert any(a == "account_session_lost" and "wrong_account" in d for a, d in audited)


def test_verify_all_skips_account_with_open_signin_window(tmp_db, monkeypatch):
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_account(aid)  # gives it a real session so verify_all won't skip it as "never"

    _mock_profile_action(monkeypatch, {"status": "signin_open=yes\nin_use=yes"})
    out = accounts.verify_all(reason="test")
    assert out == []  # skipped, not "checked and failed"
    assert accounts.get_account(aid)["verify_status"] == "verified"  # untouched


# ---------------------------------------------------------------- persistence across a process restart

def test_state_survives_reopening_the_database(tmp_db, monkeypatch):
    """Nothing accounts.py tracks lives in a Python-level variable - it's
    all in the sqlite file db.py opens fresh on every call. Simulate a
    dashboard.service restart by re-running init_db() (exactly what
    happens on process startup) against the same file and confirm the
    verified state and pinned identity are unchanged."""
    aid = accounts.create_account("Main")
    _mock_profile_action(monkeypatch, {"verify": _signed_in("main@gmail.com"), "close": "closed", "status": "signin_open=no\nin_use=no"})
    accounts.verify_account(aid)
    before = accounts.get_account(aid)

    db.init_db()  # what app startup does - must be a no-op on already-migrated data
    after = accounts.get_account(aid)
    assert after == before
