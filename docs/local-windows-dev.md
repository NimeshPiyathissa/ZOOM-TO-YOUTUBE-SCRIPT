# Local Windows development (EduMix on your laptop)

Run the dashboard and the EduMix page on **Windows 11, localhost only** - no VPS, no AWS, no WSL. It is the same
application from this repository (`feature/edumix-live`); only the Linux process layer is swapped for a small local
adapter (`dashboard/app/localdev.py`, inert unless the dev server installs it).

```
zs-dev start ─► dashboard on http://127.0.0.1:<port>   (first free port from 8765)
                 └─ EduMix page (Oxblood)  ◄── JPEG frames over DevTools (127.0.0.1:9223) ──┐
                                                                                            │
                 Chrome window on YOUR desktop, isolated profile, real mjphysics.lk ───────┘
                 you type phone number + OTP there (official site only)
```

## Commands (from `dev\windows`, or add it to PATH)
| Command | What it does |
|---|---|
| `zs-dev setup` | once: virtualenv + dependencies under `%LOCALAPPDATA%\ZoomToYT-Dev` |
| `zs-dev start [-Open]` | start the dashboard; prints the URL; first start prints the `admin` password **once** |
| `zs-dev stop` | stop the dashboard and close **only** the EduMix browser window |
| `zs-dev restart` | stop + start |
| `zs-dev status` | running? URL, browser window, DevTools port |
| `zs-dev logs [-Follow]` | the redacted diagnostic log |
| `zs-dev doctor` | dependencies, Chrome, ports, capture flag, paths |
| `zs-dev reset-password` | new admin password (shown once) |
| `zs-dev reset-session` | forget the saved MJPhysics login (asks first) |
| `zs-dev test` | run the automated tests with the virtualenv |
| `zs-dev clean` | delete everything under `%LOCALAPPDATA%\ZoomToYT-Dev` (asks first) |

`zs-dev` = `zs-dev.cmd` (works without changing the PowerShell execution policy). Options: `-Port N`, `-ChromePort N`.

## The workflow
1. `zs-dev start -Open`, log in as `admin` with the printed password.
2. Sidebar → **EduMix**. Paste your MJPhysics class URL → **Open class**.
3. A Chrome window opens on your desktop with the real site. If you are not signed in, the page says **Sign-in
   needed**: type your phone number there, then the OTP when it asks. **Nothing you type passes through the dashboard.**
4. Once signed in the dashboard takes the browser to your class by itself (switch off with the checkbox) and shows the
   state: signed in / no live class / loading / playing / paused / buffering / session expired / protected playback /
   offline / browser unreachable. The preview mirrors the window; try **Fit / Fill / Reset / Fullscreen**, **Reconnect**
   and **Stop**.
5. Close the browser window yourself to see browser recovery: the supervisor notices and reopens it after a grace
   period (press **Stop** first if you want it to stay closed).

## What is saved, and where (all under `%LOCALAPPDATA%\ZoomToYT-Dev`)
* `chrome-profiles\edumix` - the isolated browser profile. Your MJPhysics cookies live here, encrypted by Windows for
  your account (DPAPI). The dashboard never reads them. While the site's session is valid you will not be asked to sign
  in again, across restarts. `zs-dev reset-session` deletes it.
* `data\` - local dashboard database (a hash of the admin password, the saved EduMix source URL, a "session last valid"
  timestamp). No OTPs, passwords, cookies or tokens.
* `logs\dashboard.log` - diagnostics with phone numbers, OTP/password/token pairs, Cookie/Authorization headers and URL
  query strings masked. (`logs\server.err` holds only startup crashes.)
* Nothing is written inside the git repository.

## Safety
* **Capture and YouTube broadcasting are off**: `EDUMIX_CAPTURE_ENABLED=False` (the adapter refuses to install otherwise),
  `ffmpeg-stream` cannot start locally, the YouTube card is locked, and protected playback is reported and never captured.
* The adapter **never runs `sudo`/`systemctl`** - every such call is a no-op. Zoom, YouTube and other Linux features are
  therefore *simulated/non-functional* here; they are not what this mode is for.
* It only ever opens `https://mjphysics.lk/...` (or `about:blank`), only ever closes processes that carry its own
  `--user-data-dir`, and never touches your everyday Chrome. DevTools listens on 127.0.0.1 only.
* No production setting, server or deployment is read or changed; no network call goes anywhere but your browser to the
  site you asked for. It does not bypass login, OTP, CAPTCHA, DRM or any access control.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `Python 3.10+ was not found` | install Python from python.org (tick *Add to PATH*), run `zs-dev setup` |
| `Chrome/Edge not found` | install Chrome, or set `ZS_CHROME_PATH` to `chrome.exe` / `msedge.exe` |
| `The dashboard did not come up` | the script prints the last error lines; also `zs-dev doctor`, `zs-dev logs` |
| `DevTools port 9223 is already in use` | `zs-dev start -ChromePort 9224` |
| Lost the admin password | `zs-dev reset-password` |
| Preview shows "Live view disconnected" | the browser window was closed: press **Reconnect** |
| Site shows CAPTCHA / blocks the login | stop. Complete it yourself in the window if the site allows; never automate it |
| Logged out unexpectedly | the site's session expired (or it allows one device): sign in again; `reset-session` for a clean start |

## What cannot be tested without a real live class
First-time OTP login and expiry wording on the real site; the live player's technology, autoplay, aspect ratio and
whether playback is protected; page-level Fit/Fill on the real player; real class-state transitions (live → ended);
audio, A/V sync and everything downstream of capture (disabled). See `docs/edumix-live-checklist.md`.
