# Encryption design (Part 0)

This document explains what is encrypted at rest on the VPS, how, and — just as
importantly — what it does and doesn't protect against. Read this before deciding
which unlock mode to run in.

## What's encrypted

Every credential this project ever stores moves through the vault, not plaintext
config files, once setup has run:

- YouTube stream key
- Zoom meeting passcode, and any `pwd=`/`tk=` tokens embedded in saved Zoom links
- The ZoomBot VNC password
- Telegram bot token, API hash, and session string (if the Telegram integration is
  configured)

Non-secret configuration — resolution, bitrate, bot display name, which source is
active, sign-in mode, timezone — stays in plain files (`.env`'s non-secret keys,
`current-source.env`, `settings.json`'s non-secret fields) exactly as before. Only
values that were genuinely secret move into the vault; this isn't a general-purpose
config store.

The dashboard admin login is a **separate credential**, hashed with argon2
(`app/security.py`) the same way it always was. It is not part of the vault and
isn't affected by any of the below — losing the master password doesn't lock you
out of the dashboard UI itself, only out of the secrets the vault holds.

## KDF and cipher

- **Key derivation**: argon2id (`app/crypto.py`), via the `argon2-cffi` dependency
  already used for the admin login. Parameters: time_cost=3, memory_cost=256 MiB,
  parallelism=2 — deliberately heavier than the admin-login hasher's defaults, since
  this only runs once per unlock, not on every request. Benchmarked at roughly
  0.5–1.5s on the project's documented minimum spec (4 vCPU / 8GB, no GPU).
- **Encryption**: AES-256-GCM (via the `cryptography` package), an authenticated
  cipher — a wrong master password, or any tampering with the stored ciphertext, is
  detected via the AEAD authentication tag failing to verify, not by comparing
  against a stored password hash. There is nothing on disk that lets an attacker
  test a password guess any faster than actually attempting a full decrypt.
- **Salt**: a random 16-byte salt is generated once per install (`vault init`) and
  stored alongside the ciphertext (never the password itself). Changing the master
  password generates a fresh salt and re-encrypts everything under a new key.

## Unlock modes

Two modes, chosen at setup and changeable later (`vault set-unlock-mode`):

### `prompt` (nothing on disk)

Nothing usable to decrypt the vault is ever written to disk. After a reboot, or
any process restart, the vault stays locked until an administrator submits the
master password again. **Strongest** — a stolen disk image or backup contains only
authenticated ciphertext, useless without the password, which exists nowhere on
the machine. **Downside**: the stream cannot auto-recover after a reboot without
someone manually unlocking it first.

### `cached` (default)

The derived key is cached at `/etc/zoom-stream/master.key`, owned `root:dashboard`,
mode `0640` — root can write it (only `vault init`/`change-master-password`/
`set-unlock-mode`, run via `sudo`, ever do), and the `dashboard` service account can
read it, because the *running dashboard process* (which is not root) is what needs
to auto-unlock at every boot. There is no way to satisfy both "root-only, 0600" and
"an unprivileged service auto-unlocks unattended" at the same time — this mode
picks the second, and says so plainly.

**Honest limitation**: this protects against a stolen disk image, a leaked backup,
or a compromise that doesn't have root — not against an attacker who already has
root on the box, since root can always read a file that's group-readable by the
account it's about to impersonate anyway. If your threat model is specifically "a
disk snapshot or backup leaks," cached mode still fully protects you. If it's "an
attacker gets root on the live box," neither mode helps — at that point they can
read the decrypted secrets directly out of the running dashboard process's memory
regardless of how the key got there.

A manual `vault lock` always works in either mode: it drops the in-memory key for
the current process and writes a sentinel file that blocks cached-mode auto-unlock
on the next process start, until an administrator proves they still hold the master
password by unlocking again. Locking can't delete the root-owned cache file itself
(the dashboard process doesn't have permission to) — so a stolen disk plus a stolen
cache file still only gets an attacker back to where cached mode already was; the
sentinel's job is to stop an *unattended restart* from silently reopening a vault
someone just told it to close, not to change what a full-disk theft already exposes.

## Changing the master password

```
sudo /home/dashboard/venv/bin/python -m app.cli vault change-master-password
```

Prompts for the current password (verified via the same AEAD-tag mechanism — a
wrong current password changes nothing) and a new one. Every secret is decrypted
under the old key and re-encrypted under a freshly derived key with a new random
salt in a single operation; if cached mode is active, the key cache file is
rewritten to match. There's no partial state — either the whole store is
re-encrypted under the new password, or (on any failure) nothing changes at all.

## Recovery if the master password is lost

There is no cryptographic backdoor, on purpose — that's the entire point of using
an authenticated KDF-derived key instead of a recoverable scheme. If the master
password is genuinely lost:

```
sudo /home/dashboard/venv/bin/python -m app.cli vault reset --confirm
```

This irreversibly discards the encrypted store and the key cache. Every secret —
stream key, Zoom passcode, VNC password, Telegram credentials — has to be re-entered
from scratch via `vault init` or `setup` afterward. This is the only way forward.
Say so plainly to whoever's running this: **write the master password down
somewhere durable (a password manager) the moment you set it.**

## What an attacker with root can and cannot obtain

| Attacker has...                                   | Can obtain                          | Cannot obtain |
|-----------------------------------------------------|--------------------------------------|----------------|
| A stolen disk image or backup, prompt mode           | Nothing usable — ciphertext only     | Any secret, without the master password |
| A stolen disk image or backup, cached mode           | The cached key → every secret        | — |
| Root on the live, running box (either mode)          | Every secret (reads the key cache, or the process's own memory) | — |
| The dashboard admin login only (no root, no SSH)     | Whatever the authenticated UI exposes (masked stream key, etc.) — not raw secrets | Raw secret values, the vault file's plaintext |

The takeaway: this system protects secrets **at rest** — on a disk that gets
copied, stolen, or backed up without also copying a live root session. It does not
and cannot protect against a fully compromised, currently-root-accessible machine;
no software-only secrets manager can, since the decrypted values must exist in that
machine's memory to be used at all. Reduce that risk with normal server hardening
(SSH key auth only, `ufw` locked to SSH, keeping the box patched) — this vault is
one layer, not the whole story.
