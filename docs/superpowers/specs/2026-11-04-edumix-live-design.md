# EduMix (MJPhysics) live source - design

Approved 2026-11-04. Scope of this spec: the player-independent stages. Capture and
YouTube broadcasting are intentionally out of scope until a real live class is inspected.

## Decisions

* **Architecture A** - EduMix is a new source type on the existing browser pipeline
  (`browser-source` Chrome kiosk on Xvfb `:99`), with an isolated profile
  (`chrome-profiles/edumix`). The existing encoder, audio sink, YouTube auth and noVNC
  proxy are reused, not rebuilt.
* **Interactive login (Option A)** - the real site's login is shown in the dashboard via the
  existing noVNC view; the operator types phone number and OTP. No OTP/CAPTCHA automation.
* **Player-agnostic** - a generic page probe + state classifier; a player adapter can be
  added later without touching the GUI/auth code.
* **Capture gated** - `EDUMIX_CAPTURE_ENABLED=False`; `control.unit_action` refuses to start
  `ffmpeg-stream` while the active source is EduMix, and switching to EduMix while live is refused.
* Rights: the operator operates the classes; a per-source `rights_confirmed` acknowledgement
  is recorded but never enables capture by itself.

## Units

| Unit | Purpose | Depends on |
|---|---|---|
| `app/edumix.py` | URL validation, probe JS, `classify()`, `Reconnector`, `backoff_delay`, `should_auto_navigate`, `redact`, capture gate, CDP helpers | `config`, `cdp` |
| `app/edumix_api.py` | `/api/edumix/*` routes + `Supervisor` loop | `edumix`, `control`, `sources`, `cdp`, `db` |
| `sources.py` / `control.py` | `edumix` type, option validation, env lines, producer unit, view-only switching | existing |
| `scripts/browser-source.sh` | `SOURCE_TYPE=edumix` launch, isolated 700 profile, host re-check | existing script |
| `templates/edumix.html`, `static/js/edumix.js`, `static/css/edumix.css` | Oxblood page | `vnc-embed.js`, `app.js` |

## Out of scope (next stage, after live-class inspection)

Authorized capture method, audio routing/monitoring, A/V sync and stream-health telemetry,
YouTube channel/title/privacy/start-stop, duplicate-broadcast guard for EduMix.
