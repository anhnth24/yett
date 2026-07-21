"""Image HTTP backends cho `image_gen` (spec P3 §5.3, WP3.6).

Transport injectable (`http_post` / `http_get`) → test offline không gọi mạng. Backend
thật mặc định là OpenAI-compatible Images API (`POST .../images/generations`). Host API
(và host URL ảnh nếu `response_format=url`) phải nằm trong egress allowlist; redirect bị
từ chối (không forward Authorization); DNS resolve một lần + pin IP public.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
from urllib.parse import urlparse

from yett.errors import UserFacingError

# http_post(url, headers, body) -> (status_code, response_dict)
HttpPost = Callable[[str, dict[str, str], dict], Awaitable[tuple[int, dict]]]
# http_get(url, headers) -> (status_code, body_bytes, content_type)
HttpGetBytes = Callable[[str, dict[str, str]], Awaitable[tuple[int, bytes, str]]]

_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_OPENAI_HOST = "api.openai.com"
_MAX_JSON_BYTES = 15_000_000
_MAX_IMAGE_BYTES = 10_000_000
_TIMEOUT_SEC = 60.0
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_ALLOWED_SIZES = frozenset(
    {"256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"}
)


@dataclass(frozen=True)
class GeneratedImage:
    """Ảnh đã decode — chỉ bytes + MIME đã sniff; không chứa secret."""

    data: bytes
    mime: str
    revised_prompt: str = ""


ImageGenFn = Callable[..., Awaitable[GeneratedImage]]


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    domains = (str(d).lower().rstrip(".") for d in allowlist)
    return any(d and (host == d or host.endswith("." + d)) for d in domains)


def _resolve_public(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UserFacingError(f"[DENIED] không resolve được image API host '{host}'") from e
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
        if addr.is_global and not addr.is_multicast:
            return ip
    raise UserFacingError(
        f"[DENIED] image API host '{host}' chỉ resolve ra IP nội bộ/metadata"
    )


def sniff_image_mime(data: bytes) -> str:
    """Nhận diện MIME từ magic bytes — không tin Content-Type / phần mở rộng."""
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise UserFacingError("[DENIED] image_gen trả nội dung không phải ảnh hợp lệ")


def decode_b64_image(b64: str, *, max_bytes: int = _MAX_IMAGE_BYTES) -> GeneratedImage:
    """Decode base64 → bytes + sniff MIME. Malformed / oversize → DENIED."""
    if not isinstance(b64, str) or not b64.strip():
        raise UserFacingError("[DENIED] image_gen thiếu dữ liệu ảnh base64")
    try:
        raw = base64.b64decode(b64.strip(), validate=True)
    except (ValueError, TypeError) as e:
        raise UserFacingError("[DENIED] image_gen base64 không hợp lệ") from e
    if len(raw) > max_bytes:
        raise UserFacingError(f"[DENIED] image_gen ảnh vượt {max_bytes} byte")
    if not raw:
        raise UserFacingError("[DENIED] image_gen ảnh rỗng")
    mime = sniff_image_mime(raw)
    return GeneratedImage(data=raw, mime=mime)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, host: str, pinned_ip: str, port: int, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _SafeJsonPost:
    """HTTPS JSON POST cho request mang API key — không follow redirect."""

    def __init__(
        self,
        allowlist: list[str],
        *,
        timeout: float = _TIMEOUT_SEC,
        max_body_bytes: int = _MAX_JSON_BYTES,
    ) -> None:
        self._allow = list(allowlist)
        self._timeout = timeout
        self._max_body = max_body_bytes

    async def __call__(
        self, url: str, headers: dict[str, str], body: dict
    ) -> tuple[int, dict]:
        try:
            return await asyncio.to_thread(self._post_sync, url, headers, body)
        except UserFacingError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, ValueError) as e:
            raise UserFacingError("[DENIED] lỗi mạng khi gọi image_gen") from e

    def _post_sync(
        self, url: str, headers: dict[str, str], body: dict
    ) -> tuple[int, dict]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise UserFacingError("[DENIED] image API bắt buộc dùng HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise UserFacingError("[DENIED] image API URL không được chứa credentials")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or not _host_allowed(host, self._allow):
            raise UserFacingError(
                f"[DENIED] image API host '{host or '?'}' ngoài egress allowlist"
            )
        port = parsed.port or 443
        ip = _resolve_public(host, port)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise UserFacingError("[DENIED] image_gen request không hợp lệ") from e
        status, raw = self._perform_request(host, ip, port, path, headers, payload)
        if status in _REDIRECT_CODES:
            raise UserFacingError("[DENIED] image_gen không cho phép redirect")
        if status != 200:
            return status, {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise UserFacingError("[DENIED] image_gen trả JSON không hợp lệ") from e
        if not isinstance(data, dict):
            raise UserFacingError("[DENIED] image_gen trả payload không hợp lệ")
        return status, data

    def _perform_request(
        self,
        host: str,
        ip: str,
        port: int,
        path: str,
        headers: dict[str, str],
        payload: bytes,
    ) -> tuple[int, bytes]:
        conn = _PinnedHTTPSConnection(
            host, ip, port, self._timeout, ssl.create_default_context()
        )
        try:
            conn.request("POST", path, body=payload, headers=headers)
            response = conn.getresponse()
            body = response.read(self._max_body + 1)
            if len(body) > self._max_body:
                raise UserFacingError(
                    f"[DENIED] image_gen response vượt {self._max_body} byte"
                )
            return response.status, body
        finally:
            conn.close()


class _SafeBytesGet:
    """HTTPS GET tải bytes ảnh — không follow redirect; host phải trong allowlist."""

    def __init__(
        self,
        allowlist: list[str],
        *,
        timeout: float = _TIMEOUT_SEC,
        max_body_bytes: int = _MAX_IMAGE_BYTES,
    ) -> None:
        self._allow = list(allowlist)
        self._timeout = timeout
        self._max_body = max_body_bytes

    async def __call__(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, bytes, str]:
        try:
            return await asyncio.to_thread(self._get_sync, url, headers)
        except UserFacingError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, ValueError) as e:
            raise UserFacingError("[DENIED] lỗi mạng khi tải ảnh image_gen") from e

    def _get_sync(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise UserFacingError("[DENIED] URL ảnh bắt buộc dùng HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise UserFacingError("[DENIED] URL ảnh không được chứa credentials")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or not _host_allowed(host, self._allow):
            raise UserFacingError(
                f"[DENIED] host URL ảnh '{host or '?'}' ngoài egress allowlist"
            )
        port = parsed.port or 443
        ip = _resolve_public(host, port)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        status, body, ctype = self._perform_request(host, ip, port, path, headers)
        if status in _REDIRECT_CODES:
            raise UserFacingError("[DENIED] image_gen không cho phép redirect khi tải ảnh")
        return status, body, ctype

    def _perform_request(
        self,
        host: str,
        ip: str,
        port: int,
        path: str,
        headers: dict[str, str],
    ) -> tuple[int, bytes, str]:
        conn = _PinnedHTTPSConnection(
            host, ip, port, self._timeout, ssl.create_default_context()
        )
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = response.read(self._max_body + 1)
            if len(body) > self._max_body:
                raise UserFacingError(
                    f"[DENIED] ảnh tải về vượt {self._max_body} byte"
                )
            ctype = response.getheader("Content-Type") or ""
            return response.status, body, ctype
        finally:
            conn.close()


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise UserFacingError("[DENIED] image.base_url bắt buộc dùng HTTPS")
    if not parsed.hostname:
        raise UserFacingError("[DENIED] image.base_url thiếu hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UserFacingError("[DENIED] image.base_url không được chứa credentials")
    if parsed.query or parsed.fragment:
        raise UserFacingError("[DENIED] image.base_url không được chứa query/fragment")
    return base_url.rstrip("/")


class OpenAICompatImageBackend:
    """Gọi OpenAI-compatible Images API. Key truyền vào mỗi lần gọi."""

    def __init__(
        self,
        allowlist: list[str],
        *,
        model: str = "dall-e-3",
        base_url: str | None = None,
        response_format: Literal["b64_json", "url"] = "b64_json",
        http_post: HttpPost | None = None,
        http_get: HttpGetBytes | None = None,
    ) -> None:
        self._allow = list(allowlist)
        self._model = model
        self._base = _validate_base_url(base_url or _OPENAI_DEFAULT_BASE)
        self._format: Literal["b64_json", "url"] = response_format
        self._post = http_post or _SafeJsonPost(allowlist)
        self._get = http_get or _SafeBytesGet(allowlist)

    async def __call__(
        self, prompt: str, api_key: str, *, size: str = "1024x1024"
    ) -> GeneratedImage:
        if size not in _ALLOWED_SIZES:
            raise UserFacingError(
                f"[DENIED] size không hỗ trợ — chọn một trong {sorted(_ALLOWED_SIZES)}"
            )
        host = (urlparse(self._base).hostname or "").lower().rstrip(".")
        if not host or not _host_allowed(host, self._allow):
            raise UserFacingError(
                f"[DENIED] image API host '{host or '?'}' ngoài egress allowlist — "
                f"thêm vào egress.allowlist (vd {_OPENAI_HOST})"
            )
        url = f"{self._base}/images/generations"
        body = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": self._format,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "yett-image/1.0",
        }
        try:
            status, data = await self._post(url, headers, body)
        except UserFacingError:
            raise
        except Exception as e:
            raise UserFacingError("[DENIED] lỗi mạng khi gọi image_gen") from e
        if status in (401, 403):
            raise UserFacingError(
                f"[DENIED] image_gen auth thất bại (HTTP {status}) — kiểm tra API key"
            )
        if status != 200:
            raise UserFacingError(f"[DENIED] image_gen HTTP {status}")
        return await self._parse_response(data)

    async def _parse_response(self, data: dict) -> GeneratedImage:
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise UserFacingError("[DENIED] image_gen trả payload không hợp lệ")
        first = items[0]
        if not isinstance(first, dict):
            raise UserFacingError("[DENIED] image_gen trả payload không hợp lệ")
        revised = str(first.get("revised_prompt") or "")
        if self._format == "b64_json":
            img = decode_b64_image(str(first.get("b64_json") or ""))
            return GeneratedImage(data=img.data, mime=img.mime, revised_prompt=revised)
        url = str(first.get("url") or "")
        if not url:
            raise UserFacingError("[DENIED] image_gen thiếu URL ảnh")
        return await self._download_image(url, revised_prompt=revised)

    async def _download_image(self, url: str, *, revised_prompt: str) -> GeneratedImage:
        # Không gửi Authorization khi tải URL ảnh — chỉ tải public HTTPS trên allowlist.
        try:
            status, body, _ctype = await self._get(url, {"User-Agent": "yett-image/1.0"})
        except UserFacingError:
            raise
        except Exception as e:
            raise UserFacingError("[DENIED] lỗi mạng khi tải ảnh image_gen") from e
        if status in _REDIRECT_CODES:
            raise UserFacingError("[DENIED] image_gen không cho phép redirect khi tải ảnh")
        if status != 200:
            raise UserFacingError(f"[DENIED] tải ảnh image_gen HTTP {status}")
        if len(body) > _MAX_IMAGE_BYTES:
            raise UserFacingError(f"[DENIED] ảnh tải về vượt {_MAX_IMAGE_BYTES} byte")
        if not body:
            raise UserFacingError("[DENIED] image_gen ảnh rỗng")
        mime = sniff_image_mime(body)
        return GeneratedImage(data=body, mime=mime, revised_prompt=revised_prompt)
