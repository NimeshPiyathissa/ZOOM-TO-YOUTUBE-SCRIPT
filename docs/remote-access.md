# Remote access

The dashboard binds to `127.0.0.1:8443` only — it is never directly reachable from
the internet, on purpose. `ufw` on the VPS allows inbound SSH only; everything else
(the dashboard, VNC) is loopback-bound and reached through a tunnel. Pick one of the
following depending on how you actually use this day to day.

## SSH tunnel (default, no setup required)

This is what `install.sh` assumes and what the summary panel at the end of setup
tells you to do. From your own machine:

```
ssh -i "<path to your .pem key>" -N -L 8443:127.0.0.1:8443 <ssh-user>@<vps-ip>
```

Leave that running, then open `https://127.0.0.1:8443` in a browser. Your browser
will warn about the self-signed certificate the first time — that's expected for a
loopback-only cert; accept it once.

**Trade-off**: you need a terminal open with the tunnel alive to reach the
dashboard. Fine for a laptop/desktop workflow, awkward from a phone.

## Tailscale (recommended for phone access)

[Tailscale](https://tailscale.com) puts the VPS on a private mesh network you can
reach from any device signed into the same tailnet, without opening any public
port or managing a tunnel by hand.

1. Install the Tailscale client on the VPS and sign it into your tailnet:
   `curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`.
2. Install Tailscale on your phone/laptop and sign into the same account.
3. Reach the dashboard at `https://<tailscale-hostname>:8443` from any device on
   the tailnet — no port forwarding, no tunnel command to remember.

The dashboard still only binds to `127.0.0.1`, so this doesn't change its exposure
by itself — Tailscale's own network-level access control (who's a member of your
tailnet) is what's actually gating access. Don't add anyone to the tailnet you
wouldn't trust with the dashboard.

## Caddy + a domain + real TLS (for a stable public URL)

If you want a real domain and a browser-trusted certificate instead of accepting a
self-signed one every time, put [Caddy](https://caddyserver.com) in front as a
reverse proxy. A starting point is in `dashboard/Caddyfile.example` — copy it,
point the domain at your VPS's IP, and Caddy handles Let's Encrypt automatically.

**This does expose the dashboard to the public internet** (behind Caddy's TLS and
whatever auth you add in front of it) — make sure you're comfortable with that
trade-off, keep the dashboard's own admin login strong, and consider adding an
additional layer (Caddy's own `basicauth`, or an IP allowlist) if the box is
reachable by anyone who finds the domain. This is meaningfully more exposure than
the SSH-tunnel or Tailscale options above; it's here because some setups genuinely
need a stable public URL, not because it's the recommended default.

## VNC (Remote GUI)

The dashboard's Remote GUI page proxies x11vnc through the same authenticated
dashboard session — you don't need a separate VNC client or a second tunnel. If you
do want a direct VNC client instead (TigerVNC, RealVNC), tunnel port 5900 the same
way as the dashboard port above and never expose it directly; `x11vnc` binds to
`127.0.0.1` only and this project's `ufw` rules never open it.
