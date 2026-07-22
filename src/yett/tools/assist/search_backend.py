"""Search HTTP backends cho `web_search` (spec P2 §2, WP2.8).

Transport injectable (`http_get`) → test offline không gọi mạng. Backend thật mặc định
là Brave Search API; host API phải nằm trong egress allowlist (fail-closed, cùng tinh thần
no-egress với `web_fetch`).
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
from typing import Awaitable, Callable
from urllib.parse import urlencode, urlparse

from yett.errors import UserFacingError

# http_get(url, headers) -> (status_code, response_dict)
HttpGet = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict]]]

_BRAVE_DEFAULT_BASE = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_HOST = "api.search.brave.com"
_MAX_BODY_BYTES = 2_000_000
_TIMEOUT_SEC = 15.0
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    domains = (str(d).lower().rstrip(".") for d in allowlist)
    return any(d and (host == d or host.endswith("." + d)) for d in domains)


def _resolve_public(host: str, port: int) -> str:
    """Resolve once and return the public IP used for the connection.

    Connecting to this exact IP (rather than resolving the hostname again in the HTTP
    library) closes the DNS-rebinding gap between policy check and network use.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UserFacingError(f"[DENIED] không resolve được search API host '{host}'") from e
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
        if addr.is_global and not addr.is_multicast:
            return ip
    raise UserFacingError(
        f"[DENIED] search API host '{host}' chỉ resolve ra IP nội bộ/metadata"
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a policy-checked IP while retaining hostname SNI."""

    def __init__(
        self, host: str, pinned_ip: str, port: int, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _SafeJsonGet:
    """HTTPS JSON transport for secret-bearing search requests.

    Redirects are rejected rather than followed: forwarding the API-key header to even
    another allowlisted result host would disclose the key. DNS is resolved once, checked
    for public address space, and the TCP connection is pinned to that checked address.
    """

    def __init__(
        self,
        allowlist: list[str],
        *,
        timeout: float = _TIMEOUT_SEC,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ) -> None:
        self._allow = list(allowlist)
        self._timeout = timeout
        self._max_body = max_body_bytes

    async def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, dict]:
        try:
            return await asyncio.to_thread(self._get_sync, url, headers)
        except UserFacingError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, ValueError) as e:
            # Never include the underlying exception: a transport error may echo request
            # headers, including X-Subscription-Token.
            raise UserFacingError("[DENIED] lỗi mạng khi gọi web_search") from e

    def _get_sync(self, url: str, headers: dict[str, str]) -> tuple[int, dict]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise UserFacingError("[DENIED] search API bắt buộc dùng HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise UserFacingError("[DENIED] search API URL không được chứa credentials")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or not _host_allowed(host, self._allow):
            raise UserFacingError(
                f"[DENIED] search API host '{host or '?'}' ngoài egress allowlist"
            )
        port = parsed.port or 443
        ip = _resolve_public(host, port)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        status, body = self._perform_request(host, ip, port, path, headers)
        if status in _REDIRECT_CODES:
            raise UserFacingError("[DENIED] web_search không cho phép redirect")
        if status != 200:
            return status, {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise UserFacingError("[DENIED] web_search trả JSON không hợp lệ") from e
        if not isinstance(data, dict):
            raise UserFacingError("[DENIED] web_search trả payload không hợp lệ")
        return status, data

    def _perform_request(
        self,
        host: str,
        ip: str,
        port: int,
        path: str,
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        conn = _PinnedHTTPSConnection(
            host, ip, port, self._timeout, ssl.create_default_context()
        )
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = response.read(self._max_body + 1)
            if len(body) > self._max_body:
                raise UserFacingError(
                    f"[DENIED] web_search response vượt {self._max_body} byte"
                )
            return response.status, body
        finally:
            conn.close()


class BraveSearchBackend:
    """Gọi Brave Web Search API. Key truyền vào mỗi lần gọi (không lưu lâu hơn cần thiết)."""

    def __init__(
        self,
        allowlist: list[str],
        *,
        base_url: str | None = None,
        http_get: HttpGet | None = None,
    ) -> None:
        self._allow = allowlist
        self._base = (base_url or _BRAVE_DEFAULT_BASE).rstrip("?")
        parsed = urlparse(self._base)
        if parsed.scheme != "https":
            raise UserFacingError("[DENIED] search.base_url bắt buộc dùng HTTPS")
        if not parsed.hostname:
            raise UserFacingError("[DENIED] search.base_url thiếu hostname")
        if parsed.username is not None or parsed.password is not None:
            raise UserFacingError("[DENIED] search.base_url không được chứa credentials")
        if parsed.query or parsed.fragment:
            raise UserFacingError("[DENIED] search.base_url không được chứa query/fragment")
        self._get = http_get or _SafeJsonGet(allowlist)

    async def __call__(self, query: str, api_key: str) -> list[dict]:
        host = (urlparse(self._base).hostname or "").lower().rstrip(".")
        if not host or not _host_allowed(host, self._allow):
            raise UserFacingError(
                f"[DENIED] search API host '{host or '?'}' ngoài egress allowlist — "
                f"thêm vào egress.allowlist (vd {_BRAVE_HOST})"
            )
        url = f"{self._base}?{urlencode({'q': query})}"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": "yett-search/1.0",
        }
        try:
            status, data = await self._get(url, headers)
        except UserFacingError:
            raise
        except Exception as e:
            # Injected transports are useful for tests/proxies but are not trusted to
            # produce secret-safe exception strings.
            raise UserFacingError("[DENIED] lỗi mạng khi gọi web_search") from e
        if status in (401, 403):
            # Không nhúng response body — có thể phản chiếu key/token.
            raise UserFacingError(
                f"[DENIED] web_search auth thất bại (HTTP {status}) — kiểm tra API key"
            )
        if status != 200:
            raise UserFacingError(f"[DENIED] web_search HTTP {status}")
        return _parse_brave(data)


def _parse_brave(data: dict) -> list[dict]:
    web = data.get("web") if isinstance(data, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    if not isinstance(results, list):
        return []
    out: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("description") or item.get("snippet") or ""),
            }
        )
    return out
