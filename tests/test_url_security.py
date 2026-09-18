"""Tests for app/url_security.py - the SSRF/argument-injection guard that
gates every URL before it reaches ffmpeg or Chromium argv (Change 1)."""
import socket

import pytest

from app import url_security
from app.url_security import URLSecurityError


# ---------------------------------------------------------------- schemes

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "chrome://settings",
    "chrome-extension://abc/page.html",
    "data:text/html,<script>alert(1)</script>",
    "javascript:alert(1)",
    "about:blank",
    "ftp://example.com/file",
])
def test_blocked_schemes_rejected(url):
    with pytest.raises(URLSecurityError):
        url_security.validate_url(url, "webpage")


def test_scheme_not_in_allowlist_rejected():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("gopher://example.com/", "direct")


def test_missing_scheme_rejected():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("example.com/page", "webpage")


def test_webpage_type_rejects_rtmp_scheme():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("rtmp://example.com/live", "webpage")


def test_direct_type_accepts_rtmp_scheme(monkeypatch):
    _mock_public_dns(monkeypatch)
    assert url_security.validate_url("rtmp://example.com/live", "direct") == "rtmp://example.com/live"


# ---------------------------------------------------------------- SSRF: literal IPs

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254/",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
])
def test_private_and_metadata_ip_literals_rejected(url):
    with pytest.raises(URLSecurityError):
        url_security.validate_url(url, "webpage")


def test_ipv4_mapped_ipv6_metadata_rejected():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("http://[::ffff:169.254.169.254]/", "webpage")


# ---------------------------------------------------------------- SSRF: hostnames

def test_localhost_hostname_rejected():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("http://localhost/", "webpage")


def test_localhost_subdomain_rejected():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("http://foo.localhost/", "webpage")


def test_dot_local_rejected():
    with pytest.raises(URLSecurityError):
        url_security.validate_url("http://printer.local/", "webpage")


def test_dns_rebinding_to_private_ip_rejected(monkeypatch):
    """A hostname that resolves to a private/loopback address must be
    rejected even though the hostname itself looks innocuous - this is
    the actual SSRF case (DNS rebinding), not just an IP literal typed
    directly into the URL."""
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
    monkeypatch.setattr(url_security.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(URLSecurityError):
        url_security.validate_url("http://totally-fine-looking-domain.example/", "webpage")


def test_unresolvable_host_rejected(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        raise socket.gaierror("name or service not known")
    monkeypatch.setattr(url_security.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(URLSecurityError):
        url_security.validate_url("http://this-does-not-resolve.invalid/", "webpage")


# ---------------------------------------------------------------- argument injection

@pytest.mark.parametrize("url", [
    "-rtmp://evil/",
    "--evil-flag",
    "- ",
])
def test_leading_dash_rejected(url):
    with pytest.raises(URLSecurityError):
        url_security.validate_url(url, "direct")


def test_control_characters_rejected(monkeypatch):
    _mock_public_dns(monkeypatch)
    with pytest.raises(URLSecurityError):
        url_security.validate_url("https://example.com/\r\nSet-Cookie: x", "webpage")


# ---------------------------------------------------------------- happy path

def test_valid_public_https_url_accepted(monkeypatch):
    _mock_public_dns(monkeypatch)
    result = url_security.validate_url("  https://example.com/stream  ", "webpage")
    assert result == "https://example.com/stream"


def test_valid_public_url_trailing_dot_hostname_not_treated_as_localhost(monkeypatch):
    _mock_public_dns(monkeypatch)
    assert url_security.validate_url("https://example.com./x", "webpage") == "https://example.com./x"


def _mock_public_dns(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(url_security.socket, "getaddrinfo", fake_getaddrinfo)
