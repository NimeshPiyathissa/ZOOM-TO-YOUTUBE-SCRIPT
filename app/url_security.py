"""SSRF / argument-injection guard for user-supplied source URLs (Change 1).

Every URL that will ever be handed to ffmpeg or Chromium - for a webpage
or direct-media source - must go through validate_url() first, and the
*validated* string is the only thing ever placed in an argv list (never
interpolated into a shell string; these processes are always launched via
subprocess with argv lists, never shell=True).

validate_url() is called twice in practice: once when the source is
saved (app/sources.py), and again immediately before every process launch
(app/control.py) - DNS can rebind between the two, so save-time validation
alone isn't enough to stop a TOCTOU SSRF against something like the AWS
metadata endpoint.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from . import config


class URLSecurityError(Exception):
    pass


_EXPLICITLY_BLOCKED_SCHEMES = {"file", "chrome", "chrome-extension", "data", "javascript", "about", "blob", "ftp"}


def _reject_leading_dash(value: str, field: str) -> None:
    if value.startswith("-"):
        raise URLSecurityError(f"{field} must not start with '-' (would be parsed as a command-line flag)")


def _check_ip_ranges(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, hostname: str) -> None:
    if str(ip) == "169.254.169.254":
        raise URLSecurityError("Refusing to use the cloud metadata address (169.254.169.254)")
    # IPv4-mapped IPv6 form of the metadata address, e.g. ::ffff:169.254.169.254
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        if str(ip.ipv4_mapped) == "169.254.169.254":
            raise URLSecurityError("Refusing to use the cloud metadata address (169.254.169.254)")
        ip = ip.ipv4_mapped  # re-check the mapped v4 address against the ranges below too

    if ip.is_loopback:
        raise URLSecurityError(f"{hostname!r} resolves to a loopback address - not allowed")
    if ip.is_link_local:
        raise URLSecurityError(f"{hostname!r} resolves to a link-local address - not allowed")
    if ip.is_private:
        raise URLSecurityError(f"{hostname!r} resolves to a private address ({ip}) - not allowed")
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise URLSecurityError(f"{hostname!r} resolves to a reserved/unusable address ({ip}) - not allowed")


def _resolve_and_check_host(hostname: str) -> None:
    lowered = hostname.lower().rstrip(".")
    if lowered in ("localhost",) or lowered.endswith(".localhost"):
        raise URLSecurityError("Refusing to use 'localhost' - not allowed")
    if lowered.endswith(".local"):
        raise URLSecurityError("Refusing to use a .local (mDNS) address - not allowed")

    # A bare IP literal - ip_address() handles v4 and (bracket-stripped) v6 forms.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        _check_ip_ranges(literal, hostname)
        return

    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise URLSecurityError(f"Could not resolve host {hostname!r}: {exc}")
    if not infos:
        raise URLSecurityError(f"Could not resolve host {hostname!r}: no addresses returned")
    for _family, _type, _proto, _canon, sockaddr in infos:
        _check_ip_ranges(ipaddress.ip_address(sockaddr[0]), hostname)


def validate_url(url: str, source_type: str) -> str:
    """Raises URLSecurityError if `url` isn't safe to hand to ffmpeg/Chromium
    for the given source type. Returns the trimmed URL unchanged on success -
    this is the only string that should ever be placed in argv."""
    url = (url or "").strip()
    if not url:
        raise URLSecurityError("URL is required")
    _reject_leading_dash(url, "URL")
    if any(c in url for c in "\r\n\x00"):
        raise URLSecurityError("URL must not contain control characters")

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise URLSecurityError("URL must include a scheme (e.g. https://)")
    if scheme in _EXPLICITLY_BLOCKED_SCHEMES:
        raise URLSecurityError(f"Scheme '{scheme}:' is not allowed")
    if scheme not in config.ALLOWED_URL_SCHEMES:
        raise URLSecurityError(
            f"Scheme '{scheme}:' is not allowed - only {', '.join(sorted(config.ALLOWED_URL_SCHEMES))} are permitted"
        )
    allowed_for_type = config.SCHEMES_BY_TYPE.get(source_type)
    if allowed_for_type is not None and scheme not in allowed_for_type:
        raise URLSecurityError(
            f"Scheme '{scheme}:' isn't valid for a {source_type} source - use {', '.join(sorted(allowed_for_type))}"
        )

    hostname = parts.hostname
    if not hostname:
        raise URLSecurityError("URL must include a host")

    _resolve_and_check_host(hostname)
    return url
