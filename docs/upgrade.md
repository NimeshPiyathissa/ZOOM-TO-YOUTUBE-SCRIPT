# Upgrading and uninstalling

## Upgrade

```
git pull   # or re-copy the updated folder onto the VPS
sudo ./install.sh upgrade
```

What this does:
- Re-installs system packages (idempotent — mostly no-ops if already present)
- Redeploys `scripts/`, `dashboard/app`, `dashboard/static`, `dashboard/templates`
- Reinstalls Python dependencies (`pip install -r requirements.txt`)
- Regenerates `/etc/sudoers.d/dashboard` (picks up any new allowlisted scripts)
- Reinstalls systemd unit files and **restarts `dashboard.service`** (a brief blip
  in the dashboard UI itself — the stream is not affected)

What this does **not** do:
- Touch `.env`, the encrypted vault, or any saved source/meeting data
- Restart or stop `xvfb`/`openbox`/`audio-setup`/`zoom`/`browser-source`/
  `ffmpeg-stream` — if the upgrade changed something those depend on, restart them
  yourself, on your own schedule, from the dashboard or `manage.sh`
- Re-run the Part 3 setup flow — the vault, once initialized, is left alone

After an upgrade, confirm `dashboard.service` actually picked up the new code:

```
sudo systemctl status dashboard
```

Check `Active: active (running) since <recent time>` — if the timestamp is old,
the restart step didn't take effect for some reason; `sudo systemctl restart
dashboard` by hand and check again.

## Uninstall

```
sudo ./install.sh uninstall            # keeps the vault, database, recordings
sudo ./install.sh uninstall --purge-data   # also deletes them - irreversible
```

Stops and disables every pipeline and dashboard service, removes the sudoers file
and systemd units. Without `--purge-data`, your encrypted secrets, saved sources,
and recordings are left in place under `/home/dashboard/data` and
`/home/zoombot/zoom-stream` for a future reinstall. The `zoombot` and `dashboard`
service users themselves are left in place either way (they own that data) —
remove them by hand with `deluser` only once you're certain.
