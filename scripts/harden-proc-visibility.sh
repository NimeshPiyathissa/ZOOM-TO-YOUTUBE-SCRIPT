#!/usr/bin/env bash
# Stops other local users from reading the encoder's command line (it contains the YouTube stream key: FFmpeg 6.1 cannot
# take its output URL from a file) by mounting /proc with hidepid=2 and exempting one group.
#
#   harden-proc-visibility.sh                 report only (default): what is mounted now, who would be affected. Changes nothing.
#   harden-proc-visibility.sh --apply         create the exempt group, add the service users, remount /proc, persist in fstab
#   harden-proc-visibility.sh --revert        remount /proc with hidepid=0 and remove the fstab line (group is left in place)
#
# Options:  --group NAME (default procview)   --users "dashboard ubuntu" (default)
#
# WHY A GROUP: the dashboard reads /proc/<ffmpeg pid> (owned by zoombot) for encoder CPU/progress. With a plain hidepid=2 it would
# lose that and report the encoder as missing. Members of the exempt group still see everything; root always does.
# A user must have the group in its session to benefit: SERVICES RUNNING AS THOSE USERS NEED A RESTART after --apply
# (dashboard.service for the dashboard user), otherwise they keep the old group list. That restart is deliberately NOT done
# here. Until it is done the dashboard's encoder-CPU readout may show nothing; streaming itself is unaffected.
#
# Needs root for --apply/--revert. Nothing here touches ffmpeg, systemd units or the stream key.
set -u

MODE=report
GROUP=procview
USERS="dashboard ubuntu"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply ;;
    --revert) MODE=revert ;;
    --group) shift; GROUP="${1:-procview}" ;;
    --users) shift; USERS="${1:-}" ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

current_opts() { awk '$2=="/proc"{print $4}' /proc/mounts | head -1; }
say() { printf '%s\n' "$*"; }

say "kernel : $(uname -r)"
say "/proc  : $(current_opts)"
if getent group "$GROUP" >/dev/null; then say "group  : $GROUP exists (gid $(getent group "$GROUP" | cut -d: -f3))"; else say "group  : $GROUP does not exist yet"; fi
for u in $USERS; do
  if id "$u" >/dev/null 2>&1; then
    if id -nG "$u" | tr ' ' '\n' | grep -qx "$GROUP"; then say "user   : $u is in $GROUP"; else say "user   : $u is NOT in $GROUP"; fi
  else
    say "user   : $u does not exist (skipped)"
  fi
done
say "fstab  : $(grep -E '^[^#]*[[:space:]]/proc[[:space:]]' /etc/fstab 2>/dev/null | head -1 || true)"

if [ "$MODE" = report ]; then
  say ""
  say "REPORT ONLY - nothing was changed. --apply would: create group '$GROUP', add: $USERS, remount /proc with hidepid=2,gid=<$GROUP>,"
  say "and add 'proc /proc proc defaults,hidepid=2,gid=<$GROUP> 0 0' to /etc/fstab. Restart dashboard.service afterwards."
  exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "must run as root for --$MODE" >&2; exit 2; }
grep -qw proc /proc/filesystems || { echo "no proc filesystem?" >&2; exit 2; }

if [ "$MODE" = revert ]; then
  mount -o remount,hidepid=0 /proc && say "remounted /proc with hidepid=0"
  if grep -qE '^[^#]*[[:space:]]/proc[[:space:]].*hidepid' /etc/fstab; then
    cp -a /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
    sed -i -E '/^[^#]*[[:space:]]\/proc[[:space:]].*hidepid/d' /etc/fstab && say "removed the hidepid line from /etc/fstab (backup kept)"
  fi
  exit 0
fi

# ---- apply
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
GID="$(getent group "$GROUP" | cut -d: -f3)"
for u in $USERS; do id "$u" >/dev/null 2>&1 && usermod -aG "$GROUP" "$u" && say "added $u to $GROUP"; done
mount -o "remount,hidepid=2,gid=$GID" /proc || { echo "remount failed; /proc unchanged" >&2; exit 1; }
say "remounted /proc with hidepid=2,gid=$GID"
if ! grep -qE '^[^#]*[[:space:]]/proc[[:space:]].*hidepid' /etc/fstab; then
  cp -a /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
  printf 'proc /proc proc defaults,hidepid=2,gid=%s 0 0\n' "$GID" >>/etc/fstab && say "persisted in /etc/fstab (backup kept)"
fi
say "NEXT: restart dashboard.service so the dashboard user picks up the '$GROUP' group (not done here)."
