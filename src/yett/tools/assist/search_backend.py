"""Search HTTP backends cho `web_search` (spec P2 §2, WP2.8).

Transport injectable (`http_get`) → test offline không gọi mạng. Backend thật mặc định
là Brave Search API; host API phải nằm trong egress allowlist (fail-closed, cùng tinh thần
no-egress với `web_fetch`).
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable
from urllib.parse import urlencode, urlparse

from yett.errors import UserFacingError

# http_get(url, headers) -> (status_code, response_dict)
HttpGet = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict]]]

_BRAVE_DEFAULT_BASE = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_HOST = "api.search.brave.com"


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in allowlist)


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
        self._get = http_get or _urllib_get

    async def __call__(self, query: str, api_key: str) -> list[dict]:
        host = (urlparse(self._base).hostname or "").lower()
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
            raise UserFacingError(f"[DENIED] lỗi mạng khi gọi web_search: {e}") from e
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


async def _urllib_get(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    """Transport mặc định (stdlib). Chạy trong thread để không chặn event loop."""
    import urllib.error
    import urllib.request

    def _do() -> tuple[int, dict]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {"error": str(e)}
            return e.code, payload if isinstance(payload, dict) else {"error": str(payload)}
        except urllib.error.URLError as e:
            raise UserFacingError(f"[DENIED] lỗi mạng khi gọi web_search: {e}") from e

    return await asyncio.to_thread(_do)
