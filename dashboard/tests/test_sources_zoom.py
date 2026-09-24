"""Zoom-page behaviour of the shared source store: secrets never leave
through the public view, an edit made from that view keeps them, the
revision counter moves on every write, and join options validate."""
import pytest

from app import config, db, sources, control
from app.env_store import ValidationError


@pytest.fixture(autouse=True)
def _no_real_zoombot_calls(monkeypatch):
    """_source_env_lines() reads current-source.env (to carry watermark
    config forward across a source switch) via run_as_zoombot; keep no
    real subprocess/sudo calls happening in these tests by default."""
    monkeypatch.setattr(control, "read_current_source", lambda: {})


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield


LINK = "https://us06web.zoom.us/j/81234567890?pwd=AbC123xyz"


def test_public_view_masks_everything(tmp_db):
    sid = sources.create_source("Town hall", "zoom", LINK + "#success", {"passcode": "447722", "bot_name": "Bot"})
    internal = sources.get_source(sid)
    assert internal["url"] == LINK                       # normalized: fragment gone, secret kept
    assert internal["options"]["passcode"] == "447722"
    pub = sources.public_view(internal)
    assert "AbC123xyz" not in pub["url"] and "pwd=•••" in pub["url"]
    assert "passcode" not in pub["options"] and "join_url" not in pub["options"]
    assert pub["options"]["has_passcode"] and pub["options"]["passcode_masked"] == "••••••"
    assert pub["meeting_id_formatted"] == "812 3456 7890"
    assert pub["missing"] == []
    assert "AbC123xyz" not in str(sources.list_sources_public()) and "447722" not in str(sources.list_sources_public())


def test_edit_from_public_view_keeps_secrets(tmp_db):
    sid = sources.create_source("Town hall", "zoom", LINK, {"passcode": "447722"})
    pub = sources.public_view(sources.get_source(sid))
    # what an edit form submits: the redacted URL, no passcode field
    opts = dict(pub["options"]); opts.pop("has_passcode"); opts.pop("passcode_masked"); opts.pop("has_join_url"); opts.pop("join_url_redacted")
    opts["bot_name"] = "Renamed bot"
    sources.update_source(sid, "Town hall 2", "zoom", pub["url"], opts)
    s = sources.get_source(sid)
    assert s["url"] == LINK and s["options"]["passcode"] == "447722" and s["options"]["bot_name"] == "Renamed bot"
    # an explicit new passcode replaces it
    sources.update_source(sid, "Town hall 2", "zoom", "", {"passcode": "999"})
    assert sources.get_source(sid)["options"]["passcode"] == "999"


def test_reveal_and_rev(tmp_db):
    r0 = sources.sources_rev()
    sid = sources.create_source("A", "zoom", LINK, {"passcode": "1"})
    assert sources.sources_rev() == r0 + 1
    assert sources.reveal_secrets(sid) == {"url": LINK, "passcode": "1", "join_url": ""}
    sources.set_active_source_id(sid); assert sources.sources_rev() == r0 + 2
    sources.mark_joined(sid); assert sources.sources_rev() == r0 + 3 and sources.get_source(sid)["last_joined_at"]
    copy_id = sources.duplicate_source(sid)
    assert sources.get_source(copy_id)["name"] == "A (copy)" and sources.get_source(copy_id)["options"]["passcode"] == "1"
    sources.delete_source(sid); assert sources.sources_rev() >= r0 + 5   # delete of the active source also clears active
    assert sources.get_active_source() is None


def test_zoom_missing_says_exactly_what(tmp_db):
    sid = sources.create_source("Reg", "zoom", "https://us06web.zoom.us/webinar/register/WN_abc123", {})
    s = sources.get_source(sid)
    assert s["missing"] == ["the personal join link Zoom issues after you register"] and not s["join_ready"]
    sources.set_zoom_join_url(sid, "https://us06web.zoom.us/w/81234567890?tk=abcdefghijk.LMNOP-qrs")
    s = sources.get_source(sid)
    assert s["missing"] == [] and s["join_ready"] and s["meeting_id"] == "81234567890"
    sid2 = sources.create_source("Google", "zoom", LINK, {"signin_mode": "google"})
    assert "Google account" in sources.get_source(sid2)["missing"][0]


@pytest.mark.parametrize("opts,msg", [
    ({"view": "mosaic"}, "view must be"),
    ({"rejoin_max": 99}, "rejoin_max"),
    ({"passcode": "has space"}, "Passcode may only"),
    ({"join_at": 1}, "in the past"),
    ({"meeting_kind": "party"}, "meeting_kind"),
])
def test_zoom_option_validation(tmp_db, opts, msg):
    with pytest.raises(ValidationError, match=msg):
        sources.create_source("X", "zoom", LINK, opts)


def test_vanity_must_be_resolved_first(tmp_db):
    with pytest.raises(ValidationError, match="resolved"):
        sources.create_source("Room", "zoom", "https://zoom.us/my/someone", {})


def test_source_env_lines_zoom_join_policy():
    source = {"type": "zoom", "url": LINK, "options": {"auto_rejoin": False, "rejoin_max": 3, "audio_on": True, "view": "gallery"}}
    v = control._source_env_lines(source)
    assert v["ZOOM_AUTO_REJOIN"] == "0" and v["ZOOM_REJOIN_MAX"] == "3" and v["ZOOM_AUDIO_ON"] == "1"
    assert v["ZOOM_VIDEO_ON"] == "0" and v["ZOOM_VIEW"] == "gallery" and v["ZOOM_JOIN_EPOCH"].isdigit()


def test_source_env_lines_zoom_join_via():
    assert control._source_env_lines({"type": "zoom", "url": LINK, "options": {}})["ZOOM_JOIN_VIA"] == "client"
    web = {"type": "zoom", "url": LINK, "options": {"join_method": "web"}}
    assert control._source_env_lines(web)["ZOOM_JOIN_VIA"] == "web"
    auto = {"type": "zoom", "url": LINK, "options": {"join_method": "auto"}}
    assert control._source_env_lines(auto)["ZOOM_JOIN_VIA"] == "client"   # auto always starts on the client


def test_producer_unit_for():
    assert control.producer_unit_for(None) is None
    assert control.producer_unit_for({"type": "webpage"}) == "browser-source"
    assert control.producer_unit_for({"type": "direct"}) is None
    assert control.producer_unit_for({"type": "zoom", "options": {}}) == "zoom"
    assert control.producer_unit_for({"type": "zoom", "options": {"join_method": "client"}}) == "zoom"
    assert control.producer_unit_for({"type": "zoom", "options": {"join_method": "auto"}}) == "zoom"
    assert control.producer_unit_for({"type": "zoom", "options": {"join_method": "web"}}) == "browser-source"


# ---------------------------------------------------------------- join_method / registrant_email (web-client join)

def test_join_method_and_registrant_email_validation(tmp_db):
    with pytest.raises(ValidationError, match="join_method"):
        sources.create_source("X", "zoom", LINK, {"join_method": "carrier-pigeon"})
    with pytest.raises(ValidationError, match="email"):
        sources.create_source("X", "zoom", LINK, {"registrant_email": "not-an-email"})
    sid = sources.create_source("X", "zoom", LINK, {"join_method": "web", "registrant_email": "person@example.com"})
    s = sources.get_source(sid)
    assert s["options"]["join_method"] == "web" and s["options"]["registrant_email"] == "person@example.com"


def test_registrant_email_masked_in_public_view(tmp_db):
    sid = sources.create_source("X", "zoom", LINK, {"registrant_email": "person@example.com"})
    pub = sources.public_view(sources.get_source(sid))
    assert "registrant_email" not in pub["options"]
    assert "person@example.com" not in pub["options"]["registrant_email_masked"]
    assert "@" in pub["options"]["registrant_email_masked"]


def _make_account(email, label="Acct"):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (label, profile_id, email, state, created_at) VALUES (?,?,?,?,?)",
            (label, "acct-" + label.lower(), email, "signed_in", 0.0),
        )
        return cur.lastrowid


def test_registrant_email_mismatch_warns(tmp_db):
    acct_id = _make_account("bound@example.com")
    sid = sources.create_source("X", "zoom", LINK, {"registrant_email": "registrant@example.com"})
    sources.set_account(sid, acct_id)
    s = sources.get_source(sid)
    assert s["warnings"] and any("registered with" in w for w in s["warnings"])
    assert "bound@example.com" not in " ".join(s["warnings"])   # masked, not raw
    # matching email -> no warning (account_id must be re-passed - update_source
    # doesn't preserve an unspecified account_id, same as any other field here)
    sources.update_source(sid, "X", "zoom", "", {"registrant_email": "bound@example.com"}, account_id=acct_id)
    assert sources.get_source(sid)["warnings"] == []


def test_registrant_email_without_account_warns(tmp_db):
    sid = sources.create_source("X", "zoom", LINK, {"registrant_email": "registrant@example.com"})
    s = sources.get_source(sid)
    assert any("no Google account is bound" in w for w in s["warnings"])


def test_set_last_join_method(tmp_db):
    sid = sources.create_source("X", "zoom", LINK, {})
    sources.set_last_join_method(sid, "web")
    assert sources.get_source(sid)["options"]["last_join_method"] == "web"
    sources.set_last_join_method(sid, "not-a-real-mode")
    assert sources.get_source(sid)["options"]["last_join_method"] == "web"   # unchanged, invalid ignored
