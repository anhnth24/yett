"""web_fetch (spec P0-P1). Chỉ fetch domain trong egress allowlist (no-egress guard).

Gate cũng chặn domain lạ, nhưng tool tự kiểm lần nữa (defense in depth). HTTP client
injectable để test offline (không gọi mạng thật trong test).
"""

from __future__ import annotations

from typing import Awaitable, Callable
from urllib.parse import urlparse

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult

Fetcher = Callable[[str], Awaitable[str]]


class WebFetchTool:
    """Tải nội dung một URL (chỉ domain trong allowlist)."""

    name = "web_fetch"
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    def __init__(self, allowlist: list[str], fetcher: Fetcher) -> None:
        self._allow = allowlist
        self._fetch = fetcher

    def validate(self, args: dict) -> None:
        if not args.get("url"):
            raise UserFacingError("thiếu 'url'")

    def _allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self._allow)

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        url = args["url"]
        if not self._allowed(url):
            return ToolResult.error(
                f"[DENIED] domain ngoài egress allowlist: {urlparse(url).hostname}"
            )
        content = await self._fetch(url)
        return ToolResult.success(content)
