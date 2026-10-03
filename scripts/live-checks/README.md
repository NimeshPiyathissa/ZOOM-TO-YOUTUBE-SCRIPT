# Live checks

Headless-browser and raw-protocol scripts that exercise the Remote GUI
(`/vnc`, `/remote`, `/accounts`) against a **real, already-deployed**
dashboard. These are not part of the `pytest` suite under `dashboard/tests/`
(which mocks everything and needs no live server) - they need an actual
running `dashboard.service`, a real x11vnc, and real login credentials, so
they're kept here instead and run by hand when you need to re-verify the
Remote GUI reconnect/stability behaviour after a change.

Requires `playwright` (`pip install playwright && playwright install chromium`).

All scripts read connection details from environment variables - never
hardcode a password into one of these files:

- `ZSDASH_URL` - base URL, e.g. `https://zoom.missakaart.lk` (required)
- `ZSDASH_USER` / `ZSDASH_PASS` - dashboard login (required)

## Scripts

- `vnc_hold_open.py` - opens one or more Remote GUI pages in a headless
  browser for a given duration and reports every WebSocket open/close
  event it sees, with the close code/reason. Used for the "held open with
  zero drops" class of test.

  ```
  ZSDASH_URL=https://zoom.missakaart.lk ZSDASH_USER=... ZSDASH_PASS=... \
    python vnc_hold_open.py --pages vnc --minutes 30
  python vnc_hold_open.py --pages vnc,remote,accounts --minutes 30
  ```

- `vnc_recovery_check.py` - connects to `/vnc`, then watches the page for a
  given duration while you (or another script) deliberately restart
  something server-side (x11vnc, dashboard.service), reporting every
  status-badge transition it observes (Connecting -> Reconnecting(attempt,
  delay) -> Connected) so you can confirm the client recovers on its own.

  ```
  python vnc_recovery_check.py --seconds 120
  ```
