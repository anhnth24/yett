"""HTTP fetcher SSRF-safe cho `web_fetch` (spec P0-P1, [RT-7]).

`WebFetchTool` chỉ kiểm domain của URL ĐẦU rồi delegate cho `Fetcher` injected — nó
không thấy redirect hop nên không thể tự enforce allowlist/deny theo từng hop. Module
này là lớp ĐÚNG để chặn SSRF: HTTP client tự theo dõi redirect (KHÔNG dùng auto-redirect
của `urllib`), re-check allowlist + deny IP nội bộ/metadata ở MỌI hop, và luôn connect
TCP thẳng vào IP đã resolve-và-kiểm (không resolve lại DNS lần hai) để chống DNS
rebinding (attacker đổi bản ghi DNS giữa lúc kiểm và lúc connect).

Chỉ dùng thư viện chuẩn (urllib/http.client/ssl/socket) — không thêm dependency.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlparse

from yett.errors import UserFacingError

_MAX_REDIRECTS = 5
_MAX_BODY_BYTES = 5_000_000  # 5MB — đủ cho trang doc/API response, chặn đọc vô hạn
_TIMEOUT_SEC = 15.0
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _is_denied_ip(ip: str) -> bool:
    """True nếu IP không được phép làm đích fetch: loopback (127.0.0.0/8, ::1),
    link-local + cloud metadata (169.254.0.0/16, fe80::/10), RFC1918 (10/8, 172.16/12,
    192.168/16), unique-local IPv6 (fc00::/7), CGNAT, multicast, reserved...

    Dùng `ipaddress.*.is_global` (stdlib, verify được bằng cách chạy trực tiếp trong
    môi trường này) thay vì tự tay liệt kê CIDR — `is_global` đã phủ đúng mọi dải trên
    trừ multicast (multicast trả `is_global=True` dù không phải đích TCP hợp lệ) nên
    deny thêm multicast tường minh.
    """
    addr = ipaddress.ip_address(ip.split("%", 1)[0])  # bỏ IPv6 zone id (vd fe80::1%eth0)
    return addr.is_multicast or not addr.is_global


def _allowed_host(host: str, allowlist: list[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in allowlist)


def _resolve_safe(host: str) -> str:
    """Resolve hostname -> một IP đã qua kiểm deny-list. Đây là ĐIỂM DUY NHẤT resolve
    DNS cho hop này; IP trả về được dùng thẳng để connect (không resolve lại) — chống
    rebinding (DNS trả IP khác giữa lúc kiểm và lúc thật sự nối)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UserFacingError(f"[DENIED] không resolve được host '{host}': {e}") from e
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if not _is_denied_ip(ip):
            return ip
    raise UserFacingError(f"[DENIED] '{host}' chỉ resolve ra IP nội bộ/metadata — từ chối")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection nhưng TCP connect thẳng vào `pinned_ip` (đã resolve+kiểm ở
    `_resolve_safe`) thay vì để `http.client` tự resolve lại `host` — đây là bước chống
    rebinding. Header `Host` vẫn dùng `host` gốc (http.client tự set từ `self.host`)."""

    def __init__(self, host: str, pinned_ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Như trên, cho HTTPS: TCP connect vào IP đã kiểm, TLS handshake (SNI + verify
    certificate) vẫn dùng `host` gốc — đúng chuẩn chứng chỉ, chỉ pin tầng TCP."""

    def __init__(
        self, host: str, pinned_ip: str, port: int, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # http.client.HTTPSConnection lưu context ở `_context` (private, verify bằng
        # inspect trên bản Python 3.13 dùng ở đây) chứ không phải `context` — typeshed
        # không khai báo attribute private này nên cần ignore.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class SafeHttpFetcher:
    """Fetcher SSRF-safe dùng làm `Fetcher` cho `WebFetchTool` (spec `Callable[[str],
    Awaitable[str]]`). Tự theo dõi redirect (tối đa `max_redirects` hop), re-check
    allowlist + IP nội bộ/metadata ở MỌI hop, connect thẳng IP đã kiểm (chống rebinding).
    """

    def __init__(
        self,
        allowlist: list[str],
        *,
        max_redirects: int = _MAX_REDIRECTS,
        timeout: float = _TIMEOUT_SEC,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ) -> None:
        self._allow = allowlist
        self._max_redirects = max_redirects
        self._timeout = timeout
        self._max_body = max_body_bytes

    async def __call__(self, url: str) -> str:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_sync, url)
        except UserFacingError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as e:
            # Lỗi mạng thật (connection refused, timeout, TLS...) — không phải policy
            # denial nhưng vẫn phải là lỗi agent-đọc-được, không được rơi ra ngoài
            # thành exception lạ crash turn.
            raise UserFacingError(f"[DENIED] lỗi mạng khi tải '{url}': {e}") from e

    def _fetch_sync(self, url: str) -> str:
        current = url
        for _ in range(self._max_redirects + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https"):
                raise UserFacingError(f"[DENIED] scheme không hỗ trợ: '{parsed.scheme}'")
            host = parsed.hostname or ""
            if not _allowed_host(host, self._allow):
                raise UserFacingError(f"[DENIED] domain ngoài egress allowlist: {host}")
            ip = _resolve_safe(host)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            status, headers, body = self._perform_request(host, ip, port, parsed.scheme, path)
            if status in _REDIRECT_CODES:
                location = headers.get("location")
                if not location:
                    raise UserFacingError("[DENIED] redirect thiếu Location header")
                current = urljoin(current, location)
                continue
            return body.decode("utf-8", errors="replace")
        raise UserFacingError(f"[DENIED] quá {self._max_redirects} lần redirect")

    def _perform_request(
        self, host: str, ip: str, port: int, scheme: str, path: str
    ) -> tuple[int, dict[str, str], bytes]:
        """Thực hiện 1 request GET tới `ip` (đã kiểm), Host/SNI dùng `host`. Tách thành
        method riêng (không phải hàm module) để test monkeypatch dễ mà không cần mock
        toàn bộ http.client."""
        conn: http.client.HTTPConnection
        if scheme == "https":
            conn = _PinnedHTTPSConnection(
                host, ip, port, self._timeout, ssl.create_default_context()
            )
        else:
            conn = _PinnedHTTPConnection(host, ip, port, self._timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": "yett-fetcher/1.0", "Accept": "*/*"})
            resp = conn.getresponse()
            body = resp.read(self._max_body + 1)
            if len(body) > self._max_body:
                raise UserFacingError(f"[DENIED] response vượt {self._max_body} byte — từ chối")
            headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, headers, body
        finally:
            conn.close()
