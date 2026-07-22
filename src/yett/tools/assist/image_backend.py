"""Image HTTP backends cho `image_gen` (spec P3 §5.3, WP3.6).

Transport injectable (`http_post` / `http_get`) → test offline không gọi mạng. Backend
thật mặc định là OpenAI-compatible Images API (`POST .../images/generations`). Host API
(và host URL ảnh nếu `response_format=url`) phải nằm trong egress allowlist; redirect bị
từ chối (không forward Authorization); DNS resolve một lần + pin IP public.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http.client
import ipaddress
import json
import math
import socket
import ssl
import struct
import zlib
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
_MAX_IMAGE_DIMENSION = 8_192
_MAX_IMAGE_PIXELS = 4_000_000
_MAX_DECODED_IMAGE_BYTES = 32_000_000
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


class ChargedImageError(UserFacingError):
    """Provider accepted generation, but its paid result could not be consumed safely."""


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


def _validate_download_url(url: str, allowlist: list[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UserFacingError("[DENIED] URL ảnh bắt buộc dùng HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise UserFacingError("[DENIED] URL ảnh không được chứa credentials")
    if parsed.fragment:
        raise UserFacingError("[DENIED] URL ảnh không được chứa fragment")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not _host_allowed(host, allowlist):
        raise UserFacingError(
            f"[DENIED] host URL ảnh '{host or '?'}' ngoài egress allowlist"
        )
    try:
        port = parsed.port or 443
    except ValueError as e:
        raise UserFacingError("[DENIED] URL ảnh có cổng không hợp lệ") from e
    if port != 443:
        raise UserFacingError("[DENIED] URL ảnh chỉ được dùng cổng HTTPS 443")


def _validate_dimensions(width: int, height: int) -> None:
    if (
        width <= 0
        or height <= 0
        or width > _MAX_IMAGE_DIMENSION
        or height > _MAX_IMAGE_DIMENSION
        or width * height > _MAX_IMAGE_PIXELS
    ):
        raise UserFacingError("[DENIED] image_gen ảnh có kích thước pixel không an toàn")


def _png_scanline_sizes(
    width: int, height: int, bits_per_pixel: int, interlace: int
) -> list[int]:
    if interlace == 0:
        return [((width * bits_per_pixel + 7) // 8)] * height
    passes = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    rows: list[int] = []
    for start_x, start_y, step_x, step_y in passes:
        pass_width = (
            0 if width <= start_x else (width - start_x + step_x - 1) // step_x
        )
        pass_height = (
            0 if height <= start_y else (height - start_y + step_y - 1) // step_y
        )
        if pass_width:
            rows.extend(
                [((pass_width * bits_per_pixel + 7) // 8)] * pass_height
            )
    return rows


def _validate_png_stream(compressed: bytes, scanlines: list[int]) -> None:
    expected = sum(row_bytes + 1 for row_bytes in scanlines)
    if expected > _MAX_DECODED_IMAGE_BYTES:
        raise UserFacingError("[DENIED] image_gen PNG giải nén vượt giới hạn an toàn")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected + 1)
        if len(raw) > expected or decoder.unconsumed_tail:
            raise ValueError("oversize PNG stream")
        raw += decoder.flush(expected + 1 - len(raw))
    except (ValueError, zlib.error) as e:
        raise UserFacingError("[DENIED] image_gen trả PNG hỏng") from e
    if (
        len(raw) != expected
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise UserFacingError("[DENIED] image_gen trả PNG hỏng")
    offset = 0
    for row_bytes in scanlines:
        if raw[offset] > 4:
            raise UserFacingError("[DENIED] image_gen trả PNG có filter không hợp lệ")
        offset += row_bytes + 1


def _validate_png(data: bytes) -> None:
    offset = 8
    saw_ihdr = False
    saw_idat = False
    saw_plte = False
    idat_ended = False
    color_type = -1
    scanlines: list[int] = []
    idat_parts: list[bytes] = []
    while offset < len(data):
        if len(data) - offset < 12:
            break
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data) or not all(
            ord("A") <= byte <= ord("Z") or ord("a") <= byte <= ord("z")
            for byte in kind
        ):
            break
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            break
        if not saw_ihdr:
            if kind != b"IHDR" or length != 13:
                break
            width, height = struct.unpack(">II", payload[:8])
            _validate_dimensions(width, height)
            bit_depth = payload[8]
            color_type = payload[9]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if bit_depth not in valid_depths.get(color_type, set()):
                break
            if payload[10] != 0 or payload[11] != 0 or payload[12] not in (0, 1):
                break
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
            scanlines = _png_scanline_sizes(
                width, height, channels * bit_depth, payload[12]
            )
            saw_ihdr = True
        elif kind == b"IHDR":
            break
        elif kind == b"PLTE":
            if (
                saw_plte
                or saw_idat
                or color_type in (0, 4)
                or length < 3
                or length > 768
                or length % 3
            ):
                break
            saw_plte = True
        if kind == b"IDAT":
            if idat_ended or (color_type == 3 and not saw_plte):
                break
            saw_idat = True
            idat_parts.append(payload)
        elif saw_idat and kind != b"IEND":
            idat_ended = True
        if kind == b"IEND":
            if length == 0 and saw_ihdr and saw_idat and end == len(data):
                _validate_png_stream(b"".join(idat_parts), scanlines)
                return
            break
        # Unknown critical chunks are unsafe to interpret differently across consumers.
        if kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"} and kind[:1].isupper():
            break
        offset = end
    raise UserFacingError("[DENIED] image_gen trả PNG hỏng hoặc có dữ liệu nối thêm")


_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _validate_jpeg(data: bytes) -> None:
    offset = 2
    saw_sof = False
    saw_scan = False
    while offset < len(data):
        if data[offset] != 0xFF:
            break
        marker_start = offset
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            if saw_sof and saw_scan and offset == len(data):
                return
            break
        if marker == 0xD8 or marker == 0x00:
            break
        if marker in range(0xD0, 0xD8) or marker == 0x01:
            continue
        if len(data) - offset < 2:
            break
        segment_length = struct.unpack(">H", data[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(data):
            break
        payload = data[offset + 2 : offset + segment_length]
        if marker in _JPEG_SOF_MARKERS:
            if saw_sof or len(payload) < 6:
                break
            components = payload[5]
            if (
                payload[0] not in (8, 12)
                or components not in (1, 3, 4)
                or len(payload) != 6 + 3 * components
            ):
                break
            height, width = struct.unpack(">HH", payload[1:5])
            _validate_dimensions(width, height)
            saw_sof = True
        if marker == 0xDA:
            components = payload[0] if payload else 0
            if (
                not saw_sof
                or components <= 0
                or len(payload) != 4 + 2 * components
            ):
                break
        offset += segment_length
        if marker != 0xDA:
            continue
        saw_scan = True
        # Scan entropy until a non-stuffed, non-restart marker. Progressive JPEGs can
        # contain several scans, so hand that marker back to the outer parser.
        while offset < len(data):
            marker_start = data.find(b"\xff", offset)
            if marker_start < 0:
                offset = len(data)
                break
            marker_pos = marker_start + 1
            while marker_pos < len(data) and data[marker_pos] == 0xFF:
                marker_pos += 1
            if marker_pos >= len(data):
                offset = len(data)
                break
            entropy_marker = data[marker_pos]
            if entropy_marker == 0x00 or entropy_marker in range(0xD0, 0xD8):
                offset = marker_pos + 1
                continue
            offset = marker_start
            break
    raise UserFacingError("[DENIED] image_gen trả JPEG hỏng hoặc có dữ liệu nối thêm")


def _validate_webp(data: bytes) -> None:
    if len(data) < 20 or struct.unpack("<I", data[4:8])[0] != len(data) - 8:
        raise UserFacingError("[DENIED] image_gen trả WebP hỏng hoặc có dữ liệu nối thêm")
    offset = 12
    saw_image = False
    saw_extended = False
    canvas: tuple[int, int] | None = None
    while offset < len(data):
        if len(data) - offset < 8:
            break
        kind = data[offset : offset + 4]
        length = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        payload_end = offset + 8 + length
        padded_end = payload_end + (length & 1)
        if payload_end > len(data) or padded_end > len(data):
            break
        payload = data[offset + 8 : payload_end]
        if kind == b"VP8 ":
            if (
                saw_image
                or len(payload) < 10
                or payload[0] & 0x01
                or payload[3:6] != b"\x9d\x01\x2a"
            ):
                break
            width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
            height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
            _validate_dimensions(width, height)
            if canvas is not None and canvas != (width, height):
                break
            saw_image = True
        elif kind == b"VP8L":
            if saw_image or len(payload) < 5 or payload[0] != 0x2F:
                break
            bits = int.from_bytes(payload[1:5], "little")
            if bits >> 29:
                break
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            _validate_dimensions(width, height)
            if canvas is not None and canvas != (width, height):
                break
            saw_image = True
        elif kind == b"VP8X":
            if (
                saw_extended
                or saw_image
                or len(payload) != 10
                or payload[0] & 0xC3
                or payload[1:4] != b"\0\0\0"
            ):
                break  # Reject animated images; nested ANMF parsing is intentionally absent.
            width = int.from_bytes(payload[4:7], "little") + 1
            height = int.from_bytes(payload[7:10], "little") + 1
            _validate_dimensions(width, height)
            saw_extended = True
            canvas = (width, height)
        elif kind in {b"ANIM", b"ANMF"}:
            break
        offset = padded_end
    if not saw_image or offset != len(data):
        raise UserFacingError("[DENIED] image_gen trả WebP hỏng hoặc có dữ liệu nối thêm")


def sniff_image_mime(data: bytes) -> str:
    """Nhận diện và kiểm cấu trúc ảnh; từ chối prefix/polyglot/trailing payload."""
    if not isinstance(data, bytes):
        raise UserFacingError("[DENIED] image_gen dữ liệu ảnh phải là bytes")
    if not data or len(data) > _MAX_IMAGE_BYTES:
        raise UserFacingError(f"[DENIED] image_gen ảnh vượt {_MAX_IMAGE_BYTES} byte")
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        _validate_png(data)
        return "image/png"
    if len(data) >= 4 and data.startswith(b"\xff\xd8\xff"):
        _validate_jpeg(data)
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        _validate_webp(data)
        return "image/webp"
    raise UserFacingError("[DENIED] image_gen trả nội dung không phải ảnh hợp lệ")


def decode_b64_image(b64: str, *, max_bytes: int = _MAX_IMAGE_BYTES) -> GeneratedImage:
    """Decode base64 → bytes + sniff MIME. Malformed / oversize → DENIED."""
    if not isinstance(b64, str) or not b64:
        raise UserFacingError("[DENIED] image_gen thiếu dữ liệu ảnh base64")
    if max_bytes <= 0:
        raise UserFacingError("[DENIED] image_gen giới hạn ảnh không hợp lệ")
    # Bound before decode so an injected/malformed provider cannot force a second,
    # arbitrarily large allocation. Any valid base64 for <= max_bytes fits this bound.
    max_encoded = 4 * ((max_bytes + 2) // 3)
    if len(b64) > max_encoded:
        raise UserFacingError(f"[DENIED] image_gen ảnh vượt {max_bytes} byte")
    encoded = b64.strip()
    if not encoded:
        raise UserFacingError("[DENIED] image_gen thiếu dữ liệu ảnh base64")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
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
        try:
            self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
                sock, server_hostname=self.host
            )
        except Exception:
            sock.close()
            raise


def _read_bounded_response(
    response: http.client.HTTPResponse, max_body_bytes: int
) -> bytes:
    encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise UserFacingError("[DENIED] image_gen không chấp nhận response nén")
    raw_length = response.getheader("Content-Length")
    declared: int | None = None
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError as e:
            raise UserFacingError("[DENIED] image_gen Content-Length không hợp lệ") from e
        if declared < 0:
            raise UserFacingError("[DENIED] image_gen Content-Length không hợp lệ")
        if declared > max_body_bytes:
            raise UserFacingError(
                f"[DENIED] image_gen response vượt {max_body_bytes} byte"
            )
    body = response.read(max_body_bytes + 1)
    if len(body) > max_body_bytes:
        raise UserFacingError(
            f"[DENIED] image_gen response vượt {max_body_bytes} byte"
        )
    if declared is not None and len(body) != declared:
        raise UserFacingError("[DENIED] image_gen response bị cắt ngắn")
    return body


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
            return await asyncio.wait_for(
                asyncio.to_thread(self._post_sync, url, headers, body),
                timeout=self._timeout,
            )
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
        if parsed.fragment:
            raise UserFacingError("[DENIED] image API URL không được chứa fragment")
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
            body = _read_bounded_response(response, self._max_body)
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
            return await asyncio.wait_for(
                asyncio.to_thread(self._get_sync, url, headers),
                timeout=self._timeout,
            )
        except UserFacingError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, ValueError) as e:
            raise UserFacingError("[DENIED] lỗi mạng khi tải ảnh image_gen") from e

    def _get_sync(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        _validate_download_url(url, self._allow)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or 443
        ip = _resolve_public(host, port)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        # URL ảnh là dữ liệu provider kiểm soát. Chỉ forward các header công khai cần thiết,
        # kể cả khi một caller tương lai vô tình truyền Authorization/Cookie vào transport.
        public_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() in {"accept", "user-agent"}
        }
        status, body, ctype = self._perform_request(
            host, ip, port, path, public_headers
        )
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
            body = _read_bounded_response(response, self._max_body)
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
        timeout_sec: float = _TIMEOUT_SEC,
    ) -> None:
        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise UserFacingError("[DENIED] image_gen timeout phải lớn hơn 0")
        self._allow = list(allowlist)
        self._model = model
        self._base = _validate_base_url(base_url or _OPENAI_DEFAULT_BASE)
        base_host = (urlparse(self._base).hostname or "").lower().rstrip(".")
        if not base_host or not _host_allowed(base_host, self._allow):
            raise UserFacingError(
                f"[DENIED] image API host '{base_host or '?'}' ngoài egress allowlist"
            )
        self._format: Literal["b64_json", "url"] = response_format
        self._timeout = timeout_sec
        self._post = http_post or _SafeJsonPost(allowlist, timeout=timeout_sec)
        self._get = http_get or _SafeBytesGet(allowlist, timeout=timeout_sec)

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
            response = await asyncio.wait_for(
                self._post(url, headers, body), timeout=self._timeout
            )
        except UserFacingError:
            raise
        except Exception:
            # Injected transports may put Authorization in their exception text/chain.
            raise UserFacingError("[DENIED] lỗi mạng khi gọi image_gen") from None
        if (
            not isinstance(response, tuple)
            or len(response) != 2
            or type(response[0]) is not int
            or not isinstance(response[1], dict)
        ):
            raise UserFacingError("[DENIED] image_gen transport trả payload không hợp lệ")
        status, data = response
        if status in (401, 403):
            raise UserFacingError(
                f"[DENIED] image_gen auth thất bại (HTTP {status}) — kiểm tra API key"
            )
        if status != 200:
            raise UserFacingError(f"[DENIED] image_gen HTTP {status}")
        try:
            return await self._parse_response(data)
        except UserFacingError as e:
            # A successful generation response may already be billed even when its payload
            # or subsequent URL download is malformed. Preserve that fact for the ledger.
            raise ChargedImageError(str(e)) from None

    async def _parse_response(self, data: dict) -> GeneratedImage:
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise UserFacingError("[DENIED] image_gen trả payload không hợp lệ")
        first = items[0]
        if not isinstance(first, dict):
            raise UserFacingError("[DENIED] image_gen trả payload không hợp lệ")
        revised_value = first.get("revised_prompt")
        if revised_value is not None and not isinstance(revised_value, str):
            raise UserFacingError("[DENIED] image_gen trả revised_prompt không hợp lệ")
        revised = revised_value or ""
        if len(revised) > 4_000:
            raise UserFacingError("[DENIED] image_gen trả revised_prompt quá dài")
        if self._format == "b64_json":
            encoded = first.get("b64_json")
            if not isinstance(encoded, str):
                raise UserFacingError("[DENIED] image_gen thiếu dữ liệu ảnh base64")
            img = decode_b64_image(encoded)
            return GeneratedImage(data=img.data, mime=img.mime, revised_prompt=revised)
        url = first.get("url")
        if not isinstance(url, str) or not url:
            raise UserFacingError("[DENIED] image_gen thiếu URL ảnh")
        return await self._download_image(url, revised_prompt=revised)

    async def _download_image(self, url: str, *, revised_prompt: str) -> GeneratedImage:
        # Không gửi Authorization khi tải URL ảnh — chỉ tải public HTTPS trên allowlist.
        _validate_download_url(url, self._allow)
        try:
            response = await asyncio.wait_for(
                self._get(
                    url,
                    {
                        "User-Agent": "yett-image/1.0",
                        "Accept": "image/png,image/jpeg,image/webp",
                    },
                ),
                timeout=self._timeout,
            )
        except UserFacingError:
            raise
        except Exception:
            raise UserFacingError("[DENIED] lỗi mạng khi tải ảnh image_gen") from None
        if (
            not isinstance(response, tuple)
            or len(response) != 3
            or type(response[0]) is not int
            or not isinstance(response[1], bytes)
            or not isinstance(response[2], str)
        ):
            raise UserFacingError("[DENIED] image_gen transport trả payload không hợp lệ")
        status, body, ctype = response
        if status in _REDIRECT_CODES:
            raise UserFacingError("[DENIED] image_gen không cho phép redirect khi tải ảnh")
        if status != 200:
            raise UserFacingError(f"[DENIED] tải ảnh image_gen HTTP {status}")
        if len(body) > _MAX_IMAGE_BYTES:
            raise UserFacingError(f"[DENIED] ảnh tải về vượt {_MAX_IMAGE_BYTES} byte")
        if not body:
            raise UserFacingError("[DENIED] image_gen ảnh rỗng")
        mime = sniff_image_mime(body)
        advertised = ctype.partition(";")[0].strip().lower()
        if advertised and advertised not in (mime, "application/octet-stream"):
            # Không echo header do server/provider kiểm soát vào context.
            raise UserFacingError("[DENIED] Content-Type ảnh không khớp dữ liệu thực")
        return GeneratedImage(data=body, mime=mime, revised_prompt=revised_prompt)
