"""web_search (spec P2 §2, WP2.8). API key từ secret store; kết quả fetch qua egress allowlist.

Search backend injectable để test offline. Cost span ghi vào ledger (per_call).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult

# search_fn(query, api_key) -> list of {title, url, snippet}
SearchFn = Callable[[str, str], Awaitable[list[dict]]]


class WebSearchTool:
    """Tìm kiếm web (API do người dùng cấu hình; key trong secret store)."""

    name = "web_search"
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    }

    def __init__(self, search_fn: SearchFn, secrets, api_key_secret: str) -> None:
        self._search = search_fn
        self._secrets = secrets
        self._key_name = api_key_secret

    def validate(self, args: dict) -> None:
        if not args.get("query"):
            raise UserFacingError("thiếu 'query'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        key = self._secrets.get(self._key_name)  # lấy tại điểm dùng, không log
        results = await self._search(args["query"], key)
        limit = int(args.get("limit", 5))
        lines = [f"- {r.get('title','')}\n  {r.get('url','')}\n  {r.get('snippet','')}"
                 for r in results[:limit]]
        return ToolResult.success("\n".join(lines) or "(không có kết quả)")
