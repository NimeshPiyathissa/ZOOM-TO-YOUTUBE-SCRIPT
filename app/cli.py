"""CLI entry points:
  python -m app.cli set-password [username]   (default if no subcommand)
  python -m app.cli migrate-sources           (Change 1, one-time/idempotent)
No default admin password is ever created - set-password must be run once
at install time (see install-dashboard.sh)."""
from __future__ import annotations

import argparse
import getpass
import sys
import time

from . import db, security


def cmd_set_password() -> None:
    parser = argparse.ArgumentParser(description="Set or reset the dashboard admin password")
    parser.add_argument("username", nargs="?", default="admin")
    args = parser.parse_args()

    db.init_db()
    pw1 = getpass.getpass("New password (min 12 chars): ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    if len(pw1) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        sys.exit(1)

    pw_hash = security.hash_password(pw1)
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username=?", (args.username,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE users SET password_hash=? WHERE username=?", (pw_hash, args.username))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (existing["id"],))
            print(f"Password updated for '{args.username}'. All existing sessions were revoked.")
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (args.username, pw_hash, time.time()),
            )
            print(f"Admin user '{args.username}' created.")


def cmd_migrate_sources() -> None:
    """Copies every saved `profiles` row into the new `sources` table
    (type=zoom), plus - once - whatever Zoom meeting is currently live in
    .env if it isn't already a saved profile, so nothing active is lost.
    Idempotent: safe to re-run; already-migrated rows are tracked in
    `settings` and skipped. Never touches or drops `profiles` itself."""
    from . import config, env_store  # local: avoid pulling these into set-password's import path
    from . import profiles as profiles_mod
    from . import sources as sources_mod

    db.init_db()

    def _unique_name(base: str) -> str:
        existing = {s["name"] for s in sources_mod.list_sources()}
        if base not in existing:
            return base
        n = 2
        while f"{base} ({n})" in existing:
            n += 1
        return f"{base} ({n})"

    migrated_raw = db.get_setting("migrated_profile_ids", "")
    already = {int(x) for x in migrated_raw.split(",") if x.strip()}
    created = 0

    for p in profiles_mod.list_profiles():
        if p["id"] in already:
            continue
        name = _unique_name(p["name"])
        try:
            sources_mod.create_source(name, "zoom", p["zoom_link"], {
                "passcode": p["zoom_passcode"] or "", "bot_name": p["bot_name"], "signin_mode": "guest",
            })
            already.add(p["id"])
            created += 1
            print(f"  migrated profile '{p['name']}' -> source '{name}'")
        except Exception as exc:  # noqa: BLE001 - one bad profile shouldn't abort the batch
            print(f"  SKIPPED profile '{p['name']}': {exc}", file=sys.stderr)

    db.set_setting("migrated_profile_ids", ",".join(str(i) for i in sorted(already)))

    if db.get_setting("migrated_current_env", "0") != "1":
        current = env_store.read_parsed()
        link = current.get("ZOOM_LINK", "")
        if link:
            existing_sources = sources_mod.list_sources()
            match = next((s for s in existing_sources if s["type"] == "zoom" and s["url"] == link), None)
            if match is None:
                name = _unique_name("Current meeting (migrated)")
                try:
                    sid = sources_mod.create_source(name, "zoom", link, {
                        "passcode": current.get("ZOOM_PASSCODE", ""),
                        "bot_name": current.get("BOT_NAME", "Stream Bot"),
                        "signin_mode": current.get("ZOOM_SIGNIN_MODE", "guest"),
                    })
                    sources_mod.set_active_source_id(sid)
                    created += 1
                    print(f"  captured live .env as source '{name}' and marked it active")
                except Exception as exc:  # noqa: BLE001
                    print(f"  SKIPPED capturing current .env: {exc}", file=sys.stderr)
            elif not sources_mod.get_active_source():
                sources_mod.set_active_source_id(match["id"])
                print(f"  live .env already matches source '{match['name']}' - marked it active")
        db.set_setting("migrated_current_env", "1")

    print(f"Done. {created} source(s) created this run ({len(already)} profile(s) migrated total). "
          f"The 'profiles' table was left untouched.")


def main() -> None:
    # Deliberately minimal dispatch: cmd_set_password()'s own argparse call
    # is untouched (its sys.argv handling is exactly what every existing
    # install/README instruction already invokes - see the module
    # docstring - and must keep behaving identically), so only a
    # recognized new subcommand is intercepted here.
    if len(sys.argv) > 1 and sys.argv[1] == "migrate-sources":
        cmd_migrate_sources()
    else:
        cmd_set_password()


if __name__ == "__main__":
    main()
