"""Saved meeting profiles (name + link + passcode + bot name) so you can
switch meetings in one click. Stored in the dashboard's own sqlite db
(0600, dashboard-user-owned) - not the same trust boundary as the live
.env, but still only ever returned to the authenticated admin."""
from __future__ import annotations

import time

from . import db
from .env_store import ValidationError, validate_zoom_link


def list_profiles() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_profile(profile_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None


def create_profile(name: str, zoom_link: str, zoom_passcode: str, bot_name: str) -> int:
    name = name.strip()
    if not name or len(name) > 64:
        raise ValidationError("Profile name must be 1-64 characters")
    validate_zoom_link(zoom_link)
    now = time.time()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO profiles (name, zoom_link, zoom_passcode, bot_name, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, zoom_link.strip(), zoom_passcode.strip(), bot_name.strip(), now, now),
        )
        return cur.lastrowid


def update_profile(profile_id: int, name: str, zoom_link: str, zoom_passcode: str, bot_name: str) -> None:
    name = name.strip()
    if not name or len(name) > 64:
        raise ValidationError("Profile name must be 1-64 characters")
    validate_zoom_link(zoom_link)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE profiles SET name=?, zoom_link=?, zoom_passcode=?, bot_name=?, updated_at=? WHERE id=?",
            (name, zoom_link.strip(), zoom_passcode.strip(), bot_name.strip(), time.time(), profile_id),
        )


def delete_profile(profile_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
