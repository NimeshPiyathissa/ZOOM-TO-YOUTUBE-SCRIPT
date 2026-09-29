"""SQLite storage: users, sessions, saved meeting profiles, login attempts,
audit log, and small key/value settings (auto-recovery toggle, webhook url,
schedule). Single-writer app (one uvicorn worker) so plain sqlite3 with WAL
is more than enough - no ORM, no extra service to run."""
from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    zoom_link TEXT NOT NULL,
    zoom_passcode TEXT,
    bot_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Generic source model (Change 1). Superset of what `profiles` covered
-- (Zoom-only); `profiles` is kept, untouched, as a migration safety net -
-- see app/cli.py's migrate-sources command and app/sources.py.
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,          -- 'zoom' | 'webpage' | 'direct'
    url TEXT NOT NULL,
    options TEXT NOT NULL,       -- JSON blob, type-specific (see app/sources.py)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    ip TEXT NOT NULL,
    username TEXT NOT NULL,
    ts REAL NOT NULL,
    success INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER,
    action TEXT NOT NULL,        -- 'go_live' or 'stop'
    hour INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    days_of_week TEXT NOT NULL,  -- comma list, 0=Mon .. 6=Sun (APScheduler cron convention)
    enabled INTEGER NOT NULL DEFAULT 1
);

-- Saved YouTube URLs/playlists for the touch remote's video switcher
-- (Part 3). `url` is the original pasted link (shown back to the admin);
-- the embed URL actually navigated to is derived from it fresh each time
-- (app/youtube.py), never stored, so a change to that logic applies to
-- already-saved links too.
-- Google accounts for interactive sign-in (Part 2). Holds NO password,
-- token or cookie - only a label, which zoombot-owned Chrome profile
-- directory backs it (app/accounts.py + scripts/chrome-account.sh), the
-- email Google reported at the last verification, and that result.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    label TEXT UNIQUE NOT NULL,
    profile_id TEXT UNIQUE NOT NULL,
    email TEXT,
    state TEXT NOT NULL DEFAULT 'never',   -- never | signed_in | signed_out | inconclusive
    last_verified_at REAL,
    last_result TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_links (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at REAL NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the initial release: CREATE TABLE IF NOT EXISTS won't
# retrofit an existing table, so these run as idempotent ALTER TABLEs
# guarded by a pragma check. Never destructive, never drops/renames.
_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, "ALTER TABLE ... " to run if the column is missing)
    ("schedules", "source_id", "ALTER TABLE schedules ADD COLUMN source_id INTEGER"),
    # Part 3: which Google account (Chrome profile) a source plays/joins as.
    ("sources", "account_id", "ALTER TABLE sources ADD COLUMN account_id INTEGER"),
    # Zoom page: when this source was last switched to / joined.
    ("sources", "last_joined_at", "ALTER TABLE sources ADD COLUMN last_joined_at REAL"),
    # YouTube deck on /remote: what kind of link it is, oEmbed metadata
    # cached at save time, per-link playback options, play history.
    ("youtube_links", "kind", "ALTER TABLE youtube_links ADD COLUMN kind TEXT"),
    ("youtube_links", "title", "ALTER TABLE youtube_links ADD COLUMN title TEXT"),
    ("youtube_links", "author", "ALTER TABLE youtube_links ADD COLUMN author TEXT"),
    ("youtube_links", "thumbnail_url", "ALTER TABLE youtube_links ADD COLUMN thumbnail_url TEXT"),
    ("youtube_links", "options", "ALTER TABLE youtube_links ADD COLUMN options TEXT"),
    ("youtube_links", "last_played_at", "ALTER TABLE youtube_links ADD COLUMN last_played_at REAL"),
    ("youtube_links", "plays", "ALTER TABLE youtube_links ADD COLUMN plays INTEGER NOT NULL DEFAULT 0"),
]


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


def init_db() -> None:
    with contextlib.closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        _apply_column_migrations(conn)
        conn.commit()
    # dashboard.db holds password hashes and profile data - keep it private.
    try:
        Path(config.DB_PATH).chmod(0o600)
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def audit(username: str | None, action: str, detail: str = "", ip: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, username, action, detail, ip) VALUES (?,?,?,?,?)",
            (time.time(), username, action, detail, ip),
        )


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
