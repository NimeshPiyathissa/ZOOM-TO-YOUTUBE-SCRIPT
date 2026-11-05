# Laptop-independence test plan (read-only, no broadcast)

Goal: prove the stream does not depend on the operator's laptop or browser being open, **without** stopping, restarting or
starting anything on the production VPS. Every step below only reads. Nothing here needs, or is allowed, to change a service.

## What "independent" means here

| Component | Where it runs | Runs under | Depends on the laptop? |
|-----------|---------------|------------|------------------------|
| Encoder (`ffmpeg-stream`) | VPS | zoombot's lingering user manager | No |
| Zoom client / browser source | VPS, virtual display `:99` | zoombot user manager | No |
| Dashboard (`dashboard.service`) | VPS | system service | Only as a remote control |
| Remote screen (noVNC) | VPS, via the dashboard proxy | system service | Only while someone is watching it |
| Operator's browser tab | the laptop | - | Only a viewer/controller |

The dashboard is a *controller*: closing it must not change what the encoder is doing. The one coupling to rule out is a
unit with a `PartOf=`/`BindsTo=`/`Requires=` relationship to the dashboard, or a process that is a child of an SSH/VNC session.

## Read-only checks (run as `ubuntu`, all safe while live)

1. **Ownership and lingering** - the encoder must not belong to a login session:
   `loginctl show-user zoombot -p Linger` (expect `Linger=yes`) and
   `systemctl --user -M zoombot@ show ffmpeg-stream -p Slice,ControlGroup,MainPID` - the cgroup must be under `user@<uid>.service`, not a `session-N.scope`.
2. **No dependency on the dashboard** - `systemctl show dashboard -p WantedBy,RequiredBy,BoundBy,ConsistsOf` and the same for the
   zoombot units (`-p Requires,BindsTo,PartOf,After`). The dashboard must not appear in the encoder's `Requires/BindsTo/PartOf`.
3. **No SSH/VNC parent** - `ps -o pid,ppid,sess,tty,cmd -u zoombot` : ffmpeg/zoom/chrome must have `tty=?` and a systemd parent, not `sshd`/`x11vnc` client sessions.
   (VNC server `x11vnc` serves the display; a viewer disconnecting does not touch it.)
4. **No client-side keepalive** - grep the dashboard for anything that stops the encoder when no browser is connected:
   `grep -rnE "no (clients|viewers)|idle.*stop" /home/dashboard/app/app/*.py` (expect nothing that stops units).
5. **What a lost laptop changes** - the dashboard preview and the polling loops are viewer-driven; the only server-side jobs are the
   watchdog (read-only now: it never restarts anything) and the framing watcher (does not touch the encoder).

## Evidence to collect without touching anything

* Timeline from the existing audit log: `sqlite3 -readonly` on the dashboard DB is **not** needed; use the dashboard's own Logs page for
  `login`, `logout` and `stream` events and compare with the encoder's `ActiveEnterTimestamp` (`systemctl --user -M zoombot@ show ffmpeg-stream -p ActiveEnterTimestamp`).
  An encoder whose start time precedes the operator's login and is unchanged after logout was not tied to it.
* `journalctl -u dashboard --since "-24h" | grep -iE "stop|restart"` against the encoder start time: the dashboard's restarts must not coincide with encoder restarts.

## Live proof (REQUIRES YOUR APPROVAL - not part of the automated work)

Only if you want a positive demonstration, during a stream you started yourself and are happy to disturb:

1. Note encoder `ActiveEnterTimestamp` and `MainPID`.
2. Close the laptop browser tab / log out of the dashboard / disconnect Wi-Fi for 2-3 minutes.
3. Reconnect: `MainPID` and `ActiveEnterTimestamp` must be unchanged and YouTube must show no gap.
4. (Separately, only with approval) `sudo systemctl restart dashboard` - the encoder must keep its PID. This restarts a production
   service, so it is **not** done in the local V2 work.

## Pass criteria

* Encoder and Zoom units are in zoombot's lingering manager, with no `Requires/BindsTo/PartOf` link to the dashboard or to an SSH/VNC session.
* A dashboard restart does not restart the encoder, and (V2) cannot start one: `ffmpeg-stream` starts only with a valid one-time Go Live grant.
* After a **VPS reboot** nothing broadcasts until someone presses Go Live (V2 behaviour; verify `systemctl --user -M zoombot@ is-enabled ffmpeg-stream` is `disabled`).
