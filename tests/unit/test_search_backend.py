"""Adversarial tests for the real web_search transport; no external network calls."""

from __future__ import annotations

import json
import socket

import pytest

from yett.errors import UserFacingError
from yett.tools.assist.search_backend import BraveSearchBackend, _SafeJsonGet


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )


async def test_real_transport_pins_checked_ip_and_sends_key_only_in_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    backend = BraveSearchBackend(["api.search.brave.com", "example.com"])
    transport = backend._get
    assert isinstance(transport, _SafeJsonGet)
    captured: dict = {}

    def fake_request(host, ip, port, path, headers):
        captured.update(host=host, ip=ip, port=port, path=path, headers=headers)
        payload = {
            "web": {
                "results": [
                    {
                        "title": "docs",
                        "url": "https://example.com/a",
                        "description": "safe",
                    }
                ]
            }
        }
        return 200, json.dumps(payload).encode()

    monkeypatch.setattr(transport, "_perform_request", fake_request)
    rows = await backend("hello world", "SECRET")

    assert captured["host"] == "api.search.brave.com"
    assert captured["ip"] == "93.184.216.34"
    assert "q=hello+world" in captured["path"]
    assert captured["headers"]["X-Subscription-Token"] == "SECRET"
    assert "SECRET" not in captured["path"]
    assert rows[0]["url"] == "https://example.com/a"


async def test_real_transport_rejects_redirect_without_forwarding_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    backend = BraveSearchBackend(["api.search.brave.com", "attacker.example"])
    transport = backend._get
    assert isinstance(transport, _SafeJsonGet)
    calls = 0

    def redirect(host, ip, port, path, headers):
        nonlocal calls
        calls += 1
        return 302, b""

    monkeypatch.setattr(transport, "_perform_request", redirect)
    with pytest.raises(UserFacingError, match="redirect"):
        await backend("q", "SECRET")
    assert calls == 1


async def test_real_transport_denies_private_dns_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))
        ],
    )
    backend = BraveSearchBackend(["api.search.brave.com"])
    transport = backend._get
    assert isinstance(transport, _SafeJsonGet)
    monkeypatch.setattr(
        transport,
        "_perform_request",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("request must not run for metadata/private IP")
        ),
    )
    with pytest.raises(UserFacingError, match="nội bộ/metadata"):
        await backend("q", "SECRET")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.search.brave.com/res/v1/web/search",
        "file://api.search.brave.com/etc/passwd",
        "https://user:pass@api.search.brave.com/search",
        "https://api.search.brave.com/search?existing=1",
        "https://api.search.brave.com/search#fragment",
    ],
)
def test_backend_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(UserFacingError, match="DENIED"):
        BraveSearchBackend(["api.search.brave.com"], base_url=base_url)


async def test_injected_transport_exception_cannot_reflect_secret() -> None:
    async def unsafe_get(url: str, headers: dict) -> tuple[int, dict]:
        raise RuntimeError(f"failed with headers {headers}")

    backend = BraveSearchBackend(["api.search.brave.com"], http_get=unsafe_get)
    with pytest.raises(UserFacingError, match="lỗi mạng") as exc:
        await backend("q", "UNUSUAL_SECRET_VALUE")
    assert "UNUSUAL_SECRET_VALUE" not in str(exc.value)
