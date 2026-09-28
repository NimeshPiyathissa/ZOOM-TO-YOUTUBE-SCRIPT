#!/usr/bin/env bash
# Per-account Chrome profiles for interactive Google sign-in (dashboard
# Accounts page). Each account gets its own user-data-dir under
# ~/.config/chrome-profiles/<id>, mode 0700, owned by zoombot - the
# dashboard user never reads it (cookies live there); it only invokes
# this script through its narrow sudoers rule.
#
# NO CREDENTIAL AUTOMATION. This script never types, stores, reads or
# submits a password, 2FA code, cookie or OAuth token. "signin" only puts
# a normal Chrome window on :99 at Google's own sign-in page for a human
# to complete over noVNC. "verify" only asks Google, with the profile's
# own cookies, whether a session exists - and reports what Google says.
#
# Why the sign-in window has NO --remote-debugging-port and NO --app/
# --kiosk: a DevTools-attached browser is exactly what trips Google's
# "This browser or app may not be secure" refusal. The YouTube switcher's
# kiosk Chrome (browser-source.sh) does use the debug port - that's fine
# once the profile already holds a session; the sign-in itself must
# happen in a browser Google considers ordinary. (Chrome 153 also flags
# --disable-blink-features=AutomationControlled as an unsupported switch
# with a yellow warning bar, so that is gone too - a plain window needs
# nothing of the kind.)
#
# Persistence is the profile directory itself (Cookies, Login Data) with
# --password-store=basic on every launch, so the same fixed key encrypts
# cookies whether the kiosk, the sign-in window or the verifier opened
# the profile - no keyring on this headless box.
set -euo pipefail
cd /   # never depend on the caller's cwd (the dashboard's is unreadable to zoombot)
export DISPLAY="${DISPLAY:-:99}"
# The zoombot user session has a bus (systemd --user); the dashboard's
# sudo environment doesn't carry the address, and Chrome without it logs
# a wall of "Could not parse server address" and can't reach portals.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

PROFILES_ROOT="$HOME/.config/chrome-profiles"
STREAM_PROFILE="$HOME/.config/stream-chrome-profile"
BACKUP_ROOT="$HOME/zoom-stream/backups/profiles"
CHROME_BIN="$(command -v google-chrome-stable || command -v google-chrome || echo /usr/bin/google-chrome-stable)"
LOG_DIR="$HOME/zoom-stream/logs"
ACTION="${1:-}"
ID="${2:-}"

if [[ ! "$ID" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "bad profile id" >&2
  exit 2
fi
DIR="$PROFILES_ROOT/$ID"
PIDFILE="$DIR/.signin.pid"
umask 077
mkdir -p "$LOG_DIR"

# A UA that matches the installed Chrome exactly (Google serves the
# "HeadlessChrome" UA a different, sign-in-less layout).
chrome_version() { "$CHROME_BIN" --version 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)+' | head -n1; }
ua() { echo "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/$(chrome_version | cut -d. -f1).0.0.0 Safari/537.36"; }

signin_pid() { [[ -f "$PIDFILE" ]] && cat "$PIDFILE" 2>/dev/null || true; }
signin_running() { local p; p="$(signin_pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

# Chrome leaves a SingletonLock symlink (target "host-pid") while any
# Chrome has the profile open - e.g. browser-source.sh's kiosk once a
# source is bound to this account. Stale after a crash, so check the pid.
profile_in_use() {
  local target pid
  [[ -L "$DIR/SingletonLock" ]] || return 1
  target="$(readlink "$DIR/SingletonLock" 2>/dev/null || true)"
  pid="${target##*-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

close_signin() {
  local p
  if signin_running; then
    p="$(signin_pid)"
    kill -TERM "$p" 2>/dev/null || true
    # SIGTERM lets Chrome flush cookies/session to disk; only escalate if
    # it genuinely hangs.
    for _ in $(seq 1 40); do kill -0 "$p" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
}

# Best-effort: maximize the window this profile just opened (Openbox
# ignores --start-maximized), so the sign-in form fills the preview.
maximize_window() {
  local wid
  for _ in $(seq 1 24); do
    wid="$(wmctrl -lx 2>/dev/null | grep -F "chrome-profiles/$ID" | awk '{print $1; exit}')"
    [[ -n "$wid" ]] && break
    sleep 0.25
  done
  [[ -n "$wid" ]] || return 0
  wmctrl -i -r "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null || true
  wmctrl -i -a "$wid" 2>/dev/null || true
}

launch_window() {
  # An ordinary Chrome window: no kiosk, no DevTools, no automation
  # switches, real UA. $1 = URL.
  rm -f "$DIR/SingletonLock" "$DIR/SingletonSocket" "$DIR/SingletonCookie"
  nohup "$CHROME_BIN" \
    --user-data-dir="$DIR" --password-store=basic \
    --no-first-run --no-default-browser-check \
    --window-position=0,0 \
    --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
    --disable-features=Translate,TranslateUI,Notifications \
    --lang=en-US \
    "$1" >>"$LOG_DIR/accounts.log" 2>&1 &
  echo $! > "$PIDFILE"
  maximize_window
}

require_free() {
  if signin_running; then close_signin; fi
  if profile_in_use; then
    echo "profile is currently open by another Chrome (a bound webpage source?) - switch away from it first" >&2
    exit 1
  fi
}

# What a session actually is: "Local State" (profile registry) plus the
# Default profile minus its caches. Everything else at the profile root
# (component/model stores, Safe Browsing data, metrics) is re-downloaded
# by Chrome, so import/backup copy only this - a few MB, not 200.
SESSION_PATHS=("Local State" "First Run" "Default")
CACHE_EXCLUDES=(--exclude='Singleton*' --exclude='*.pma' --exclude='Default/Cache' --exclude='Default/Code Cache'
                --exclude='Default/GPUCache' --exclude='Default/DawnCache' --exclude='Default/Service Worker'
                --exclude='Default/blob_storage' --exclude='Default/Session Storage' --exclude='Default/Extensions'
                --exclude='Default/optimization_guide_hint_cache_store' --exclude='Default/shared_proto_db'
                --exclude='Default/Shared Dictionary' --exclude='.signin.pid' --exclude='.window.pid' --exclude='*-journal')

copy_session() {   # $1 = source profile dir, $2 = destination dir (exists, empty)
  local src="$1" dst="$2" p
  for p in "${SESSION_PATHS[@]}"; do
    [[ -e "$src/$p" ]] || continue
    rsync -a "${CACHE_EXCLUDES[@]}" "$src/$p" "$dst/"
  done
}

case "$ACTION" in
  create)
    mkdir -p "$DIR"
    chmod 700 "$PROFILES_ROOT" "$DIR"
    echo "created"
    ;;

  signin)
    [[ -d "$DIR" ]] || { echo "profile does not exist - create it first" >&2; exit 1; }
    if signin_running; then maximize_window; echo "already-open"; exit 0; fi
    if profile_in_use; then
      echo "profile is currently open by another Chrome (a bound webpage source?) - switch away from it first" >&2
      exit 1
    fi
    launch_window "https://accounts.google.com/"
    echo "opened"
    ;;

  open)
    # Same ordinary window as signin, at an arbitrary https URL - used
    # for Zoom webinar registration forms, which the admin completes by
    # hand over noVNC. Only http(s); the dashboard has already run the
    # URL through its blocklist before we get here.
    URL="${3:-}"
    case "$URL" in
      http://*|https://*) ;;
      *) echo "only http(s) URLs can be opened" >&2; exit 1 ;;
    esac
    [[ -d "$DIR" ]] || { mkdir -p "$DIR"; chmod 700 "$PROFILES_ROOT" "$DIR"; }
    require_free
    launch_window "$URL"
    echo "opened"
    ;;

  close)
    close_signin
    echo "closed"
    ;;

  status)
    signin_running && echo "signin_open=yes" || echo "signin_open=no"
    profile_in_use && echo "in_use=yes" || echo "in_use=no"
    [[ -d "$DIR" ]] && echo "exists=yes" || echo "exists=no"
    [[ -f "$DIR/Default/Cookies" ]] && echo "has_cookies=yes" || echo "has_cookies=no"
    ;;

  verify)
    # Prints exactly one JSON line. Never prints cookies or page content
    # beyond the account identity. Loads myaccount.google.com, which only
    # renders for a signed-in session (otherwise Google redirects to its
    # sign-in page), in a headless Chrome with the real UA.
    if signin_running; then
      echo '{"status":"inconclusive","reason":"sign-in window is still open - finish or cancel it first"}'; exit 0
    fi
    if profile_in_use; then
      echo '{"status":"inconclusive","reason":"profile is open in another Chrome right now (the running web source) - checked through it instead","in_use":true}'; exit 0
    fi
    [[ -d "$DIR" ]] || { echo '{"status":"inconclusive","reason":"profile directory missing"}'; exit 0; }
    TMP="$(mktemp)"
    trap 'rm -f "$TMP"' EXIT
    timeout 60 "$CHROME_BIN" --headless=new --user-data-dir="$DIR" --password-store=basic \
      --disable-gpu --no-first-run --no-default-browser-check --timeout=25000 --lang=en-US \
      --user-agent="$(ua)" \
      --dump-dom "https://myaccount.google.com/?hl=en" >"$TMP" 2>/dev/null || true
    python3 - "$TMP" <<'PY'
import html, json, re, sys
dom = open(sys.argv[1], encoding="utf-8", errors="replace").read()
title = re.search(r"<title>([^<]*)</title>", dom)
title = html.unescape(title.group(1)).strip() if title else ""
m = re.search(r'aria-label="Google Account:(.*?)"', dom, re.S)
email = None
if m:
    e = re.search(r"\(([^()\s]+@[^()\s]+)\)", html.unescape(m.group(1)))
    email = e.group(1) if e else None
if not email and "Google Account" in title:
    e = re.search(r'"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"', dom)
    email = e.group(1) if e else None
if email and ("Google Account" in title or "myaccount" in dom[:4000]):
    print(json.dumps({"status": "signed_in", "email": email, "title": title})); sys.exit(0)
if len(dom) < 500:
    print(json.dumps({"status": "inconclusive", "reason": "no page returned (network/timeout)"})); sys.exit(0)
if re.search(r"may not be secure|unusual activity|verify it.s you|Confirm it.s you|/challenge/", dom, re.I):
    print(json.dumps({"status": "inconclusive", "reason": "Google is asking for extra verification on this session - open Sign in and complete the prompt", "title": title})); sys.exit(0)
# No identity: Google's sign-in page (redirect), or its signed-out
# landing page offering "Sign in" links to accounts.google.com.
if re.search(r"accounts\.google\.com/(v3/)?signin|ServiceLogin|Sign in - Google Accounts", dom + title)         or (not email and "accounts.google.com" in dom):
    print(json.dumps({"status": "signed_out", "title": title})); sys.exit(0)
print(json.dumps({"status": "inconclusive", "reason": "unexpected page: " + (title or "untitled"), "title": title}))
PY
    ;;

  import)
    # Copy the shared stream profile's session (the one Zoom's Google
    # sign-in and the kiosk have been using) into this account, without
    # its caches. Source is fixed - no arbitrary paths from the caller.
    [[ -d "$STREAM_PROFILE/Default" ]] || { echo "the stream profile has no Default profile yet" >&2; exit 1; }
    require_free
    rm -rf "$DIR"; mkdir -p "$DIR"; chmod 700 "$PROFILES_ROOT" "$DIR"
    copy_session "$STREAM_PROFILE" "$DIR"
    chmod -R go-rwx "$DIR"
    echo "imported"
    ;;

  signout)
    # Forget the session: the profile becomes a fresh, empty one (same
    # directory, same id). Nothing is read from it first.
    require_free
    rm -rf "$DIR"; mkdir -p "$DIR"; chmod 700 "$DIR"
    echo "signed-out"
    ;;

  backup)
    require_free
    [[ -d "$DIR/Default" ]] || { echo "nothing to back up - the account has never signed in" >&2; exit 1; }
    mkdir -p "$BACKUP_ROOT"; chmod 700 "$HOME/zoom-stream/backups" "$BACKUP_ROOT"
    OUT="$BACKUP_ROOT/$ID-$(date +%Y%m%d-%H%M%S).tar.gz"
    STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
    copy_session "$DIR" "$STAGE"
    tar -C "$STAGE" -czf "$OUT" .
    chmod 600 "$OUT"
    # keep the three newest per account
    ls -1t "$BACKUP_ROOT/$ID-"*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
    echo "backup=$(basename "$OUT") size=$(stat -c %s "$OUT")"
    ;;

  restore)
    # Restore the newest backup of this same id (or a named one under
    # the backup dir - basename only, no paths).
    require_free
    NAME="${3:-}"
    if [[ -n "$NAME" ]]; then
      [[ "$NAME" =~ ^[a-z0-9-]+-[0-9]{8}-[0-9]{6}\.tar\.gz$ && "$NAME" == "$ID-"* ]] || { echo "bad backup name" >&2; exit 1; }
      SRC="$BACKUP_ROOT/$NAME"
    else
      SRC="$(ls -1t "$BACKUP_ROOT/$ID-"*.tar.gz 2>/dev/null | head -n1 || true)"
    fi
    [[ -n "$SRC" && -f "$SRC" ]] || { echo "no backup found for this account" >&2; exit 1; }
    rm -rf "$DIR"; mkdir -p "$DIR"; chmod 700 "$DIR"
    tar -C "$DIR" -xzf "$SRC"
    chmod -R go-rwx "$DIR"
    echo "restored=$(basename "$SRC")"
    ;;

  backups)
    ls -1t "$BACKUP_ROOT/$ID-"*.tar.gz 2>/dev/null | while read -r f; do echo "$(basename "$f") $(stat -c %s "$f") $(stat -c %Y "$f")"; done
    ;;

  remove)
    close_signin
    if profile_in_use; then
      echo "profile is open by another Chrome - switch away from it first" >&2; exit 1
    fi
    rm -rf "$DIR"
    rm -f "$BACKUP_ROOT/$ID-"*.tar.gz 2>/dev/null || true
    echo "removed"
    ;;

  *)
    echo "usage: $(basename "$0") create|signin|close|status|verify|import|signout|backup|restore|backups|remove <profile-id>  |  open <profile-id> <https-url>  |  restore <profile-id> [backup-name]" >&2
    exit 2
    ;;
esac
