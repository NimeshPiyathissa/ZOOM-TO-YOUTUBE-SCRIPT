# Remote access

By default, the dashboard listens on `0.0.0.0:443` — reachable directly at
`https://<vps-ip>` from any browser, no SSH tunnel or port-forward command
required. `ufw` allows inbound SSH and `443/tcp`; `x11vnc` itself still stays
loopback-only and is only ever reached through the dashboard's own authenticated
proxy, never directly.

## Direct HTTPS (default)

```
https://<vps-ip>
```

Log in with the admin username/password set at install time. Your browser will
warn about the certificate being self-signed the first time — that's unavoidable
without a real domain (Let's Encrypt doesn't issue certificates for a bare IP
address). The certificate's SAN is generated to match your VPS's actual IP, so
it's a normal "self-signed" warning, not a hostname-mismatch one; accept it once.

**What actually protects the login page**, since it's directly internet-facing:
TLS (so credentials aren't sent in the clear), CSRF protection, and an account
lockout after 5 failed login attempts within a 15-minute window
(`app/config.py`'s `LOGIN_MAX_FAILURES`/`LOGIN_LOCKOUT_WINDOW_SECONDS`). This is a
real trade-off, made deliberately to avoid needing a tunnel command at all — see
[security.md](security.md) if you want the more locked-down alternative below
instead.

## More locked down: loopback-only + SSH tunnel

If you'd rather the login page not be reachable by internet scanners/bots at all
(the strictly more conservative option, at the cost of needing a running SSH
tunnel to reach it):

1. Edit `systemd/dashboard.service`'s `ExecStart`: change `--host 0.0.0.0` to
   `--host 127.0.0.1`.
2. `sudo ufw delete allow 443/tcp` (and close it in your cloud provider's
   Security Group / firewall console too, if you opened it there).
3. `sudo systemctl restart dashboard`.
4. From your own machine:
   ```
   ssh -i "<path to your key>" -N -L 443:127.0.0.1:443 <ssh-user>@<vps-ip>
   ```
   Leave that running, then open `https://127.0.0.1` in a browser.

## Tailscale (private network, works well from a phone without a tunnel)

[Tailscale](https://tailscale.com) puts the VPS on a private mesh network you can
reach from any device signed into the same tailnet, without opening any public
port or managing a tunnel by hand — a middle ground between the two options
above.

1. Install the Tailscale client on the VPS and sign it into your tailnet:
   `curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`.
2. Install Tailscale on your phone/laptop and sign into the same account.
3. If you also want the dashboard *off* the public internet, follow the
   loopback-only steps above, then reach it at
   `https://<tailscale-hostname>` from any device on the tailnet instead of the
   public IP. If you're fine with the default direct-HTTPS exposure, Tailscale
   isn't necessary at all — `https://<vps-ip>` already works from anywhere.

## Caddy + a domain + a browser-trusted certificate

If you want a real domain and a certificate browsers won't warn about (instead of
accepting the self-signed one), put [Caddy](https://caddyserver.com) in front. A
starting point is in `dashboard/Caddyfile.example`:

1. Edit `systemd/dashboard.service`'s `ExecStart`: drop `--ssl-keyfile`/
   `--ssl-certfile` and change `--port 443` to `--port 8000` (Caddy terminates
   TLS now, not uvicorn directly).
2. Set `TRUST_FORWARDED_PROTO = True` in `app/config.py` and redeploy — this
   makes the session cookie's `Secure` flag trust Caddy's `X-Forwarded-Proto`
   header instead of assuming the app itself is doing TLS.
3. Install Caddy, copy `dashboard/Caddyfile.example` to `/etc/caddy/Caddyfile`,
   replace the placeholder domain, point that domain's DNS at your VPS,
   `systemctl restart caddy`. Caddy handles Let's Encrypt automatically.
4. `sudo systemctl restart dashboard`.

Firewall-wise this is no different from the default (443 already open) — the
only thing that changes is who terminates TLS and whether the certificate is
self-signed or CA-issued.

## VNC (Remote GUI)

The dashboard's Remote GUI page proxies x11vnc through the same authenticated
dashboard session — you don't need a separate VNC client or any tunnel for it,
regardless of which option above you use. `x11vnc` itself binds to `127.0.0.1`
only and is never opened in the firewall directly.
