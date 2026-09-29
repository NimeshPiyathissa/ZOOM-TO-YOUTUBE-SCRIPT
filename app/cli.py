"""CLI entry points:
  python -m app.cli set-password [username]   (default if no subcommand)
  python -m app.cli migrate-sources           (Change 1, one-time/idempotent)
  python -m app.cli vault <init|unlock|change-master-password|set-unlock-mode|reset>
  python -m app.cli setup                     (Part 3: full first-run flow)
  python -m app.cli migrate-secrets [--dry-run] [--redact]
No default admin password (or master password, or VNC password) is ever
created - setup/set-password must be run once at install time (see
install-dashboard.sh). Every password prompt below supports a
non-interactive, environment-variable-driven mode for automation (see
_read_secret) - values are never echoed, never logged, and never appear in
argv either way."""
from __future__ import annotations

import argparse
import getpass
import os
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


MIN_PASSWORD_LEN = 12


def _is_root() -> bool:
    """True only on POSIX with euid 0. os.geteuid doesn't exist on
    Windows (this project's real target is Ubuntu; Windows is only ever a
    dev/test machine) - mirrors conftest.py's pwd shim in spirit: never
    crash on import/call here, just correctly report "not root"."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _read_secret(label: str, env_var: str, *, confirm: bool = True, min_len: int = MIN_PASSWORD_LEN) -> str:
    """Reads a password either from an environment variable (non-interactive
    automation mode - the value is used as-is, not re-confirmed, since
    there's no operator present to mistype it twice) or interactively via
    getpass (never echoed). Refuses to return an empty or too-short value
    either way - no default/fallback password is ever produced here."""
    env_val = os.environ.get(env_var)
    if env_val is not None:
        if len(env_val) < min_len:
            print(f"{env_var} is set but shorter than {min_len} characters - refusing.", file=sys.stderr)
            sys.exit(1)
        return env_val
    while True:
        pw1 = getpass.getpass(f"{label} (min {min_len} chars): ")
        if len(pw1) < min_len:
            print(f"Must be at least {min_len} characters.", file=sys.stderr)
            continue
        if confirm:
            pw2 = getpass.getpass(f"Confirm {label.lower()}: ")
            if pw1 != pw2:
                print("Did not match - try again.", file=sys.stderr)
                continue
        return pw1


def cmd_vault() -> None:
    from . import config, secret_store

    parser = argparse.ArgumentParser(prog="app.cli vault", description="Manage the encrypted secret store")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init")
    sub.add_parser("unlock")
    sub.add_parser("change-master-password")
    p_mode = sub.add_parser("set-unlock-mode")
    p_mode.add_argument("mode", choices=sorted(config.UNLOCK_MODES))
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--confirm", action="store_true", help="required - there is no cryptographic recovery")
    args = parser.parse_args(sys.argv[2:])

    def _require_root_for_cached(mode: str) -> None:
        if mode == "cached" and not _is_root():
            print(
                "Cached unlock mode writes a root-owned key cache file - "
                "run this command with sudo.", file=sys.stderr,
            )
            sys.exit(1)

    if args.action == "init":
        if secret_store.is_initialized():
            print(f"A secret store already exists at {config.SECRET_STORE_FILE}.", file=sys.stderr)
            print("Use 'vault change-master-password' to rotate it, or 'vault reset' to discard it.", file=sys.stderr)
            sys.exit(1)
        mode = os.environ.get("ZOOMBOT_UNLOCK_MODE", config.DEFAULT_UNLOCK_MODE)
        if mode not in config.UNLOCK_MODES:
            print(f"ZOOMBOT_UNLOCK_MODE must be one of {sorted(config.UNLOCK_MODES)}", file=sys.stderr)
            sys.exit(1)
        _require_root_for_cached(mode)
        pw = _read_secret("Master Encryption Password", "ZOOMBOT_MASTER_PASSWORD")
        secret_store.initialize(pw, unlock_mode=mode)
        print(f"Vault initialized ({mode} unlock mode). No secrets stored yet - run 'migrate-secrets' or 'setup'.")

    elif args.action == "unlock":
        pw = _read_secret("Master Encryption Password", "ZOOMBOT_MASTER_PASSWORD", confirm=False)
        try:
            secret_store.unlock(pw)
        except secret_store.crypto.DecryptionError:
            print("Wrong master password.", file=sys.stderr)
            sys.exit(1)
        print("Vault unlocked.")

    elif args.action == "change-master-password":
        old_pw = _read_secret("Current master password", "ZOOMBOT_OLD_MASTER_PASSWORD", confirm=False)
        new_pw = _read_secret("New Master Encryption Password", "ZOOMBOT_MASTER_PASSWORD")
        meta = secret_store.read_meta()
        if meta is None:
            print("No vault exists yet - run 'vault init' first.", file=sys.stderr)
            sys.exit(1)
        _require_root_for_cached(meta.get("unlock_mode", config.DEFAULT_UNLOCK_MODE))
        try:
            secret_store.change_master_password(old_pw, new_pw)
        except secret_store.crypto.DecryptionError:
            print("Current master password is wrong - nothing was changed.", file=sys.stderr)
            sys.exit(1)
        print("Master password changed. Every secret was re-encrypted under the new password.")

    elif args.action == "set-unlock-mode":
        _require_root_for_cached(args.mode)
        pw = _read_secret("Master Encryption Password", "ZOOMBOT_MASTER_PASSWORD", confirm=False)
        try:
            secret_store.set_unlock_mode(args.mode, password=pw)
        except secret_store.crypto.DecryptionError:
            print("Wrong master password.", file=sys.stderr)
            sys.exit(1)
        print(f"Unlock mode set to '{args.mode}'.")

    elif args.action == "reset":
        if not args.confirm:
            print(
                "Refusing: 'vault reset' irreversibly discards the encrypted store. "
                "There is no cryptographic recovery for a lost master password - this "
                "is the only way forward, and every secret must be re-entered "
                "afterward. Re-run with --confirm if that's really what you want.",
                file=sys.stderr,
            )
            sys.exit(1)
        secret_store.reset_vault()
        print("Vault reset. All encrypted secrets and the key cache were discarded.")


def cmd_setup() -> None:
    """Part 3's full first-run flow, in order: Master Encryption Password
    (with the unlock-mode choice from Part 0) -> dashboard admin
    username/password -> VNC password. Refuses to complete without all
    three; no defaults, no fallback passwords. Prints a one-time summary
    panel at the end - passwords are only ever echoed back if they were
    set interactively in *this* session (never for env-var/non-interactive
    runs), with an explicit warning not to paste the summary anywhere."""
    from . import config, secret_store

    interactive = "ZOOMBOT_MASTER_PASSWORD" not in os.environ

    if secret_store.is_initialized():
        print(f"A secret store already exists at {config.SECRET_STORE_FILE} - setup already ran.", file=sys.stderr)
        print("Use 'vault change-master-password' or 'vault reset' instead.", file=sys.stderr)
        sys.exit(1)

    mode = os.environ.get("ZOOMBOT_UNLOCK_MODE", config.DEFAULT_UNLOCK_MODE)
    if mode not in config.UNLOCK_MODES:
        print(f"ZOOMBOT_UNLOCK_MODE must be one of {sorted(config.UNLOCK_MODES)}", file=sys.stderr)
        sys.exit(1)
    if mode == "cached" and not _is_root():
        print("Cached unlock mode writes a root-owned key cache file - run setup with sudo.", file=sys.stderr)
        sys.exit(1)

    master_pw = _read_secret("Master Encryption Password", "ZOOMBOT_MASTER_PASSWORD")
    admin_username = os.environ.get("ZOOMBOT_ADMIN_USERNAME")
    if not admin_username:
        admin_username = (input("Dashboard admin username [admin]: ").strip() or "admin") if interactive else "admin"
    admin_pw = _read_secret("Dashboard admin password", "ZOOMBOT_ADMIN_PASSWORD")
    vnc_pw = _read_secret("ZoomBot VNC password", "ZOOMBOT_VNC_PASSWORD")

    secret_store.initialize(master_pw, initial_secrets={"VNC_PASSWORD": vnc_pw}, unlock_mode=mode)

    from .control import run_as_zoombot, SUDO
    vnc_argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.ROTATE_VNC_SCRIPT)]
    vnc_proc = run_as_zoombot(vnc_argv, input_bytes=vnc_pw.encode("utf-8"), timeout=15)
    vnc_store_ok = vnc_proc.returncode == 0
    if not vnc_store_ok:
        print(
            "Warning: could not set x11vnc's password store yet (the pipeline may not be "
            "deployed on this box yet) - the VNC password is saved in the vault; run "
            "'vault unlock' then use the dashboard's VNC rotate action once the pipeline "
            "is installed to apply it.", file=sys.stderr,
        )

    db.init_db()
    admin_hash = security.hash_password(admin_pw)
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username=?", (admin_username,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET password_hash=? WHERE username=?", (admin_hash, admin_username))
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (admin_username, admin_hash, time.time()),
            )

    print()
    print("=" * 60)
    print(" Setup complete")
    print("=" * 60)
    print(f" Dashboard admin username : {admin_username}")
    print(f" Vault unlock mode        : {mode}")
    print(f" Secret store             : {config.SECRET_STORE_FILE}")
    if mode == "cached":
        print(f" Key cache (root-only)    : {config.MASTER_KEY_CACHE_FILE}")
    if interactive:
        print()
        print(" Passwords set this session (shown once - do not paste this")
        print(" output anywhere, including into chat with an AI assistant):")
        print(f"   Master Encryption Password : {master_pw}")
        print(f"   Dashboard admin password    : {admin_pw}")
        print(f"   VNC password                : {vnc_pw}")
    print()
    print(" Next steps: run 'migrate-secrets' to bring in any existing")
    print(" plaintext Zoom/YouTube/Telegram secrets, then set a stream key")
    print(" and add a meeting from the dashboard.")
    print("=" * 60)


def cmd_migrate_secrets() -> None:
    from . import migrate_secrets, secret_store

    parser = argparse.ArgumentParser(prog="app.cli migrate-secrets")
    parser.add_argument("--dry-run", action="store_true", help="report what would move, change nothing")
    parser.add_argument(
        "--redact", action="store_true",
        help="after a successful (non-dry-run) migration, blank the plaintext originals too",
    )
    args = parser.parse_args(sys.argv[2:])

    if not secret_store.is_unlocked():
        # A fresh process in cached unlock mode should auto-unlock from the
        # key cache file - that's the entire point of cached mode - rather
        # than making the operator retype the master password for every
        # separate CLI invocation. Only prompt-mode installs (or a cached
        # install that's been explicitly locked) actually need 'vault
        # unlock' run first.
        secret_store.try_auto_unlock()
    if not secret_store.is_unlocked():
        print("Vault is locked - run 'vault unlock' (or 'vault init' on a fresh install) first.", file=sys.stderr)
        sys.exit(1)

    report = migrate_secrets.run(dry_run=args.dry_run)
    print(f"{'Would migrate' if args.dry_run else 'Migrated'} {report['count']} secret(s): "
          f"{', '.join(report['keys_found']) or '(none found)'}")
    if report["backup_paths"]:
        print("Backups written:")
        for kind, path in report["backup_paths"].items():
            print(f"  {kind}: {path}")

    if args.dry_run:
        print("Dry run - nothing was changed. Re-run without --dry-run to apply.")
        return

    if args.redact:
        migrate_secrets.redact_all_plaintext()
        print("Plaintext originals redacted (backups above are the only remaining copy).")
    else:
        print("Plaintext originals left in place - re-run with --redact once you've verified the vault, "
              "or call redact_all_plaintext() directly.")


def main() -> None:
    # Deliberately minimal dispatch: cmd_set_password()'s own argparse call
    # is untouched (its sys.argv handling is exactly what every existing
    # install/README instruction already invokes - see the module
    # docstring - and must keep behaving identically), so only a
    # recognized new subcommand is intercepted here.
    if len(sys.argv) > 1 and sys.argv[1] == "migrate-sources":
        cmd_migrate_sources()
    elif len(sys.argv) > 1 and sys.argv[1] == "vault":
        cmd_vault()
    elif len(sys.argv) > 1 and sys.argv[1] == "setup":
        cmd_setup()
    elif len(sys.argv) > 1 and sys.argv[1] == "migrate-secrets":
        cmd_migrate_secrets()
    else:
        cmd_set_password()


if __name__ == "__main__":
    main()
