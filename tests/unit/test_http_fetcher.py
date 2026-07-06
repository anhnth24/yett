"""Test SafeHttpFetcher ([RT-7] SSRF fix ở tầng HTTP client): deny IP nội bộ/metadata,
re-check allowlist mỗi redirect hop, chống DNS rebinding (connect vào IP đã kiểm, không
resolve lại), cap kích thước response. Không gọi mạng thật — monkeypatch DNS + request.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from yett.errors import UserFacingError
from yett.tools.builtin.http_fetcher import (
    SafeHttpFetcher,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    _allowed_host,
    _is_denied_ip,
    _resolve_safe,
)


# --- _is_denied_ip ---
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local / cloud metadata (AWS/GCP/Azure)
        "10.0.0.5",  # RFC1918
        "172.16.0.5",
        "172.31.255.255",
        "192.168.1.1",
        "::1",  # loopback IPv6
        "fe80::1",  # link-local IPv6
        "fc00::1",  # unique-local IPv6 (tương đương RFC1918)
        "100.64.0.1",  # CGNAT (RFC6598)
        "224.0.0.1",  # multicast
        "0.0.0.0",
    ],
)
def test_is_denied_ip_blocks_internal_and_metadata(ip: str) -> None:
    assert _is_denied_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2001:4860:4860::8888"])
def test_is_denied_ip_allows_public(ip: str) -> None:
    assert _is_denied_ip(ip) is False


def test_is_denied_ip_strips_ipv6_zone_id() -> None:
    assert _is_denied_ip("fe80::1%eth0") is True


# --- _allowed_host ---
def test_allowed_host_exact_and_subdomain() -> None:
    allow = ["example.com"]
    assert _allowed_host("example.com", allow)
    assert _allowed_host("api.example.com", allow)
    assert not _allowed_host("evil.com", allow)
    assert not _allowed_host("notexample.com", allow)  # không phải subdomain thật


# --- _resolve_safe (chống rebinding: chỉ 1 lần resolve, IP trả về đã qua kiểm) ---
def test_resolve_safe_returns_first_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _resolve_safe("example.com") == "93.184.216.34"


def test_resolve_safe_denies_when_only_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UserFacingError, match="DENIED"):
        _resolve_safe("attacker-controlled.example")


def test_resolve_safe_skips_private_picks_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS trả nhiều bản ghi (1 private trước, 1 public sau) -> vẫn chọn được IP public
    an toàn thay vì deny toàn bộ (không phải lỗ hổng — IP dùng để connect LÀ IP đã kiểm)."""

    def fake_getaddrinfo(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _resolve_safe("mixed.example") == "8.8.8.8"


def test_resolve_safe_dns_failure_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UserFacingError, match="DENIED"):
        _resolve_safe("khong-ton-tai.example")


# --- pinned connection: TCP connect PHẢI dùng IP đã kiểm, không resolve lại hostname ---
def test_pinned_http_connection_connects_to_pinned_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_create_connection(address, timeout):
        seen["address"] = address
        raise RuntimeError("dừng sau khi ghi lại address — không cần socket thật")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    conn = _PinnedHTTPConnection("evil-dns-controlled.example", "8.8.8.8", 80, 5.0)
    with pytest.raises(RuntimeError):
        conn.connect()
    assert seen["address"] == ("8.8.8.8", 80)  # KHÔNG phải hostname


def test_pinned_https_connection_connects_to_pinned_ip_keeps_sni_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    def fake_create_connection(address, timeout):
        seen["address"] = address
        return "raw-sock-stub"

    def fake_wrap_socket(self, sock, server_hostname=None):
        seen["server_hostname"] = server_hostname
        return "tls-sock-stub"

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)
    conn = _PinnedHTTPSConnection(
        "evil-dns-controlled.example", "8.8.8.8", 443, 5.0, ssl.create_default_context()
    )
    conn.connect()
    assert seen["address"] == ("8.8.8.8", 443)  # TCP connect vào IP đã kiểm
    assert seen["server_hostname"] == "evil-dns-controlled.example"  # SNI/cert vẫn đúng host


# --- SafeHttpFetcher: allowlist + redirect + rebinding ở tầng end-to-end ---
async def test_fetch_returns_body_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SafeHttpFetcher(["example.com"])
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(
        fetcher, "_perform_request",
        lambda host, ip, port, scheme, path: (200, {}, b"hello world"),
    )
    assert await fetcher("https://example.com/x") == "hello world"


async def test_fetch_denies_disallowed_redirect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hop đầu allowlist hợp lệ nhưng redirect sang domain ngoài allowlist -> phải chặn
    NGAY (WebFetchTool chỉ check hop đầu nên nếu fetcher không tự re-check thì đây là SSRF)."""
    fetcher = SafeHttpFetcher(["example.com"])
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))])

    def fake_request(host, ip, port, scheme, path):
        if host == "example.com":
            return 302, {"location": "https://evil.com/steal"}, b""
        raise AssertionError("không được request tới host ngoài allowlist")

    monkeypatch.setattr(fetcher, "_perform_request", fake_request)
    with pytest.raises(UserFacingError, match="allowlist"):
        await fetcher("https://example.com/redirect-me")


async def test_fetch_denies_redirect_to_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect trong CÙNG allowlist (DNS rebinding qua domain con) nhưng resolve ra IP
    nội bộ/metadata -> vẫn phải chặn ở hop đó."""
    fetcher = SafeHttpFetcher(["example.com"])

    def fake_getaddrinfo(host, port):
        if host == "example.com":
            return [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
        return [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]  # metadata IP

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def fake_request(host, ip, port, scheme, path):
        if host == "example.com":
            return 302, {"location": "https://internal.example.com/meta"}, b""
        raise AssertionError("không được request khi IP đã bị deny")

    monkeypatch.setattr(fetcher, "_perform_request", fake_request)
    with pytest.raises(UserFacingError, match="DENIED"):
        await fetcher("https://example.com/redirect-me")


async def test_fetch_follows_redirect_within_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SafeHttpFetcher(["example.com"])
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))])
    calls = []

    def fake_request(host, ip, port, scheme, path):
        calls.append(path)
        if path == "/a":
            return 301, {"location": "/b"}, b""
        return 200, {}, b"final content"

    monkeypatch.setattr(fetcher, "_perform_request", fake_request)
    assert await fetcher("https://example.com/a") == "final content"
    assert calls == ["/a", "/b"]


async def test_fetch_too_many_redirects_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SafeHttpFetcher(["example.com"], max_redirects=2)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(
        fetcher, "_perform_request",
        lambda host, ip, port, scheme, path: (302, {"location": "https://example.com/loop"}, b""),
    )
    with pytest.raises(UserFacingError, match="redirect"):
        await fetcher("https://example.com/loop")


async def test_fetch_rejects_non_http_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SafeHttpFetcher(["example.com"])
    with pytest.raises(UserFacingError, match="scheme"):
        await fetcher("file:///etc/passwd")


async def test_fetch_denies_first_hop_outside_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SafeHttpFetcher(["example.com"])
    with pytest.raises(UserFacingError, match="allowlist"):
        await fetcher("https://evil.com/x")


def test_perform_request_denies_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap kích thước response — không đọc/giữ body vô hạn trong bộ nhớ."""

    class _FakeResp:
        status = 200

        def read(self, n: int) -> bytes:
            return b"x" * n

        def getheaders(self):
            return []

    class _FakeConn:
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return _FakeResp()

        def close(self):
            pass

    import yett.tools.builtin.http_fetcher as hf

    monkeypatch.setattr(hf, "_PinnedHTTPConnection", _FakeConn)
    fetcher = SafeHttpFetcher(["example.com"], max_body_bytes=10)
    with pytest.raises(UserFacingError, match="DENIED"):
        fetcher._perform_request("example.com", "93.184.216.34", 80, "http", "/")


async def test_fetch_wraps_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SafeHttpFetcher(["example.com"])
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))])

    def boom(host, ip, port, scheme, path):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(fetcher, "_perform_request", boom)
    with pytest.raises(UserFacingError, match="lỗi mạng"):
        await fetcher("https://example.com/x")
