"""Adversarial tests for image_gen OpenAI-compatible transport; no live network."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import struct
import zlib

import pytest

from yett.errors import UserFacingError
from yett.tools.assist import image_backend as image_backend_mod
from yett.tools.assist.image_backend import (
    OpenAICompatImageBackend,
    _SafeBytesGet,
    _SafeJsonPost,
    _read_bounded_response,
    decode_b64_image,
    sniff_image_mime,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG).decode()
_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9k="
)
_WEBP = base64.b64decode(
    "UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA"
)
SECRET = "UNUSUAL_IMAGE_SECRET_VALUE"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )


async def test_request_mapping_posts_expected_body_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    backend = OpenAICompatImageBackend(
        ["api.openai.com"], model="dall-e-3", response_format="b64_json"
    )
    transport = backend._post
    assert isinstance(transport, _SafeJsonPost)
    captured: dict = {}

    def fake_request(host, ip, port, path, headers, payload):
        captured.update(
            host=host, ip=ip, port=port, path=path, headers=headers, payload=payload
        )
        body = {"data": [{"b64_json": _PNG_B64, "revised_prompt": "cat"}]}
        return 200, json.dumps(body).encode()

    monkeypatch.setattr(transport, "_perform_request", fake_request)
    img = await backend("a fluffy cat", SECRET, size="1024x1024")

    assert captured["host"] == "api.openai.com"
    assert captured["ip"] == "93.184.216.34"
    assert captured["path"] == "/v1/images/generations"
    assert captured["headers"]["Authorization"] == f"Bearer {SECRET}"
    req = json.loads(captured["payload"].decode())
    assert req == {
        "model": "dall-e-3",
        "prompt": "a fluffy cat",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    assert SECRET not in captured["path"]
    assert img.data == _PNG
    assert img.mime == "image/png"
    assert img.revised_prompt == "cat"


async def test_real_transport_rejects_redirect_without_forwarding_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    backend = OpenAICompatImageBackend(["api.openai.com", "attacker.example"])
    transport = backend._post
    assert isinstance(transport, _SafeJsonPost)
    calls = 0

    def redirect(host, ip, port, path, headers, payload):
        nonlocal calls
        calls += 1
        return 302, b""

    monkeypatch.setattr(transport, "_perform_request", redirect)
    with pytest.raises(UserFacingError, match="redirect"):
        await backend("q", SECRET)
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
    backend = OpenAICompatImageBackend(["api.openai.com"])
    transport = backend._post
    assert isinstance(transport, _SafeJsonPost)
    monkeypatch.setattr(
        transport,
        "_perform_request",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("request must not run for metadata/private IP")
        ),
    )
    with pytest.raises(UserFacingError, match="nội bộ/metadata"):
        await backend("q", SECRET)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.openai.com/v1",
        "file://api.openai.com/v1",
        "https://user:pass@api.openai.com/v1",
        "https://api.openai.com/v1?x=1",
        "https://api.openai.com/v1#frag",
    ],
)
def test_backend_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(UserFacingError, match="DENIED"):
        OpenAICompatImageBackend(["api.openai.com"], base_url=base_url)


async def test_injected_transport_exception_cannot_reflect_secret() -> None:
    async def unsafe_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        raise RuntimeError(f"failed with headers {headers}")

    backend = OpenAICompatImageBackend(["api.openai.com"], http_post=unsafe_post)
    with pytest.raises(UserFacingError, match="lỗi mạng") as exc:
        await backend("q", SECRET)
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None


async def test_malformed_json_and_missing_b64_denied() -> None:
    async def bad_json(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"b64_json": "!!!"}]}

    backend = OpenAICompatImageBackend(["api.openai.com"], http_post=bad_json)
    with pytest.raises(UserFacingError, match="base64|không hợp lệ"):
        await backend("q", "k")

    async def empty_data(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": []}

    backend2 = OpenAICompatImageBackend(["api.openai.com"], http_post=empty_data)
    with pytest.raises(UserFacingError, match="payload không hợp lệ"):
        await backend2("q", "k")


async def test_oversize_b64_payload_denied() -> None:
    huge = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 50).decode()

    async def big(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"b64_json": huge}]}

    # Patch decode limit indirectly by using decode_b64_image in isolation + backend default.
    with pytest.raises(UserFacingError, match="vượt"):
        decode_b64_image(huge, max_bytes=16)

    # Backend uses default 10MB — craft a non-image that fails sniff instead for unit speed.
    async def not_image(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"b64_json": base64.b64encode(b"hello").decode()}]}

    backend = OpenAICompatImageBackend(["api.openai.com"], http_post=not_image)
    with pytest.raises(UserFacingError, match="không phải ảnh"):
        await backend("q", "k")


async def test_url_mode_downloads_with_allowlist_and_rejects_unsafe_url() -> None:
    async def api_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        assert body["response_format"] == "url"
        return 200, {"data": [{"url": "https://cdn.example.com/a.png"}]}

    async def ok_get(url: str, headers: dict) -> tuple[int, bytes, str]:
        assert url == "https://cdn.example.com/a.png"
        assert "Authorization" not in headers
        return 200, _PNG, "image/png"

    backend = OpenAICompatImageBackend(
        ["api.openai.com", "cdn.example.com"],
        response_format="url",
        http_post=api_post,
        http_get=ok_get,
    )
    img = await backend("q", SECRET)
    assert img.data == _PNG

    async def evil_url(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"url": "https://evil.example/x.png"}]}

    # Real SafeBytesGet enforces allowlist before any socket I/O.
    denied = OpenAICompatImageBackend(
        ["api.openai.com"],
        response_format="url",
        http_post=evil_url,
    )
    with pytest.raises(UserFacingError, match="egress allowlist"):
        await denied("q", SECRET)

    async def redirect_get(url: str, headers: dict) -> tuple[int, bytes, str]:
        return 302, b"", ""

    redir = OpenAICompatImageBackend(
        ["api.openai.com", "cdn.example.com"],
        response_format="url",
        http_post=api_post,
        http_get=redirect_get,
    )
    with pytest.raises(UserFacingError, match="redirect"):
        await redir("q", SECRET)

    async def http_url(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"url": "http://cdn.example.com/a.png"}]}

    cleartext = OpenAICompatImageBackend(
        ["api.openai.com", "cdn.example.com"],
        response_format="url",
        http_post=http_url,
    )
    with pytest.raises(UserFacingError, match="HTTPS"):
        await cleartext("q", SECRET)


async def test_url_mode_real_get_transport_rejects_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)

    async def api_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"url": "https://cdn.example.com/a.png"}]}

    backend = OpenAICompatImageBackend(
        ["api.openai.com", "cdn.example.com"],
        response_format="url",
        http_post=api_post,
    )
    transport = backend._get
    assert isinstance(transport, _SafeBytesGet)

    def redirect(host, ip, port, path, headers):
        return 302, b"", ""

    monkeypatch.setattr(transport, "_perform_request", redirect)
    with pytest.raises(UserFacingError, match="redirect"):
        await backend("q", SECRET)


async def test_api_host_must_be_on_allowlist() -> None:
    async def boom(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        raise AssertionError("no http when host denied")

    with pytest.raises(UserFacingError, match="egress allowlist"):
        OpenAICompatImageBackend(allowlist=["example.com"], http_post=boom)


async def test_auth_failure_does_not_embed_response_body() -> None:
    async def unauthorized(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 401, {"error": f"bad key {headers.get('Authorization')}"}

    backend = OpenAICompatImageBackend(["api.openai.com"], http_post=unauthorized)
    with pytest.raises(UserFacingError, match="auth thất bại") as exc:
        await backend("q", SECRET)
    assert SECRET not in str(exc.value)
    assert "Bearer" not in str(exc.value)


def test_base64_size_is_bounded_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def should_not_decode(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversize base64 must be rejected before decode")

    monkeypatch.setattr(image_backend_mod.base64, "b64decode", should_not_decode)
    with pytest.raises(UserFacingError, match="vượt"):
        decode_b64_image("AAAAA", max_bytes=3)
    assert called is False


@pytest.mark.parametrize(
    "payload",
    [
        _PNG + b"<script>alert(1)</script>",
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff\xd9trailing",
        b"RIFF\x04\x00\x00\x00WEBP",
    ],
)
def test_structural_mime_validation_rejects_truncated_and_polyglot(payload: bytes) -> None:
    with pytest.raises(UserFacingError, match="DENIED"):
        sniff_image_mime(payload)


def test_structural_mime_validation_accepts_supported_valid_formats() -> None:
    assert sniff_image_mime(_PNG) == "image/png"
    assert sniff_image_mime(_JPEG) == "image/jpeg"
    assert sniff_image_mime(_WEBP) == "image/webp"


def test_png_rejects_crc_valid_but_invalid_compressed_stream() -> None:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    malformed = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", b"not-zlib")
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(UserFacingError, match="PNG hỏng"):
        sniff_image_mime(malformed)


def test_png_rejects_excessive_decoded_dimensions_before_inflate() -> None:
    ihdr = struct.pack(">IIBBBBB", 8192, 8192, 8, 6, 0, 0, 0)
    bomb = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b""))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(UserFacingError, match="pixel không an toàn"):
        sniff_image_mime(bomb)


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self._headers = headers

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


@pytest.mark.parametrize(
    ("headers", "match"),
    [
        ({"Content-Encoding": "gzip"}, "nén"),
        ({"Content-Length": "not-a-number"}, "Content-Length"),
        ({"Content-Length": "11"}, "vượt"),
        ({"Content-Length": "5"}, "cắt ngắn"),
    ],
)
def test_response_bounds_reject_encoding_and_bad_content_length(
    headers: dict[str, str], match: str
) -> None:
    response = _FakeResponse(b"x", headers)
    with pytest.raises(UserFacingError, match=match):
        _read_bounded_response(response, 10)  # type: ignore[arg-type]


def test_response_without_content_length_is_still_bounded() -> None:
    response = _FakeResponse(b"x" * 11, {})
    with pytest.raises(UserFacingError, match="vượt"):
        _read_bounded_response(response, 10)  # type: ignore[arg-type]


async def test_url_mode_rejects_mime_header_mismatch_and_fragment() -> None:
    async def mismatched_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"url": "https://cdn.example.com/a.png"}]}

    async def mismatched_get(url: str, headers: dict) -> tuple[int, bytes, str]:
        return 200, _PNG, "image/jpeg"

    backend = OpenAICompatImageBackend(
        ["api.openai.com", "cdn.example.com"],
        response_format="url",
        http_post=mismatched_post,
        http_get=mismatched_get,
    )
    with pytest.raises(UserFacingError, match="Content-Type") as exc:
        await backend("q", SECRET)
    assert "image/jpeg" not in str(exc.value)

    async def fragment_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"url": "https://cdn.example.com/a.png#hidden"}]}

    fragment = OpenAICompatImageBackend(
        ["api.openai.com", "cdn.example.com"],
        response_format="url",
        http_post=fragment_post,
    )
    with pytest.raises(UserFacingError, match="fragment"):
        await fragment("q", SECRET)


async def test_injected_transport_is_timed_out_and_shape_checked() -> None:
    async def hangs(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    timeout_backend = OpenAICompatImageBackend(
        ["api.openai.com"], http_post=hangs, timeout_sec=0.001
    )
    with pytest.raises(UserFacingError, match="lỗi mạng") as exc:
        await timeout_backend("q", SECRET)
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None

    async def malformed(url: str, headers: dict, body: dict):
        return "200", []

    malformed_backend = OpenAICompatImageBackend(
        ["api.openai.com"], http_post=malformed
    )
    with pytest.raises(UserFacingError, match="payload không hợp lệ"):
        await malformed_backend("q", SECRET)


def test_download_transport_strips_sensitive_headers_and_rejects_nonstandard_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    transport = _SafeBytesGet(["cdn.example.com"])
    captured: dict[str, object] = {}

    def fake_request(host, ip, port, path, headers):
        captured.update(headers=headers, port=port)
        return 200, _PNG, "image/png"

    monkeypatch.setattr(transport, "_perform_request", fake_request)
    status, body, _ctype = transport._get_sync(
        "https://cdn.example.com/a.png",
        {
            "Authorization": f"Bearer {SECRET}",
            "Cookie": f"key={SECRET}",
            "User-Agent": "test",
        },
    )
    assert status == 200 and body == _PNG
    assert captured["headers"] == {"User-Agent": "test"}
    assert SECRET not in str(captured)

    with pytest.raises(UserFacingError, match="443"):
        transport._get_sync("https://cdn.example.com:8443/a.png", {})


async def test_injected_get_cannot_bypass_url_policy() -> None:
    async def api_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        return 200, {"data": [{"url": "https://evil.example/a.png"}]}

    called = False

    async def unsafe_get(url: str, headers: dict) -> tuple[int, bytes, str]:
        nonlocal called
        called = True
        return 200, _PNG, "image/png"

    backend = OpenAICompatImageBackend(
        ["api.openai.com"],
        response_format="url",
        http_post=api_post,
        http_get=unsafe_get,
    )
    with pytest.raises(UserFacingError, match="egress allowlist"):
        await backend("q", SECRET)
    assert called is False
