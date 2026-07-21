"""web_search (spec P2 §2, WP2.8). API key từ secret store; kết quả lọc qua egress allowlist.

Search backend injectable để test offline. Cost span ghi vào ledger (per_call) qua
`span_attrs` trên ToolResult — loop gắn vào TOOL_CALL span (không chứa secret).
"""

from __future__ import annotations

from typing import Awaitable, Callable
from urllib.parse import urlparse

from yett.errors import SecretNotFound, UserFacingError
from yett.tools.base import ToolCtx, ToolResult

# search_fn(query, api_key) -> list of {title, url, snippet}
SearchFn = Callable[[str, str], Awaitable[list[dict]]]


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in allowlist)


class WebSearchTool:
    """Tìm kiếm web (API do người dùng cấu hình; key trong secret store)."""

    name = "web_search"
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    }

    def __init__(
        self,
        search_fn: SearchFn,
        secrets,
        api_key_secret: str,
        *,
        allowlist: list[str],
        cost_usd: float = 0.0,
        cost_provider: str = "brave",
        cost_model: str = "web_search",
    ) -> None:
        self._search = search_fn
        self._secrets = secrets
        self._key_name = api_key_secret
        self._allow = allowlist
        self._cost_usd = cost_usd
        self._cost_provider = cost_provider
        self._cost_model = cost_model

    def validate(self, args: dict) -> None:
        if not args.get("query"):
            raise UserFacingError("thiếu 'query'")

    def _allowed_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return _host_allowed(host, self._allow)

    def _resolve_key(self) -> str:
        """Lấy key tại điểm dùng. SecretNotFound / key rỗng → UserFacingError (fail an toàn,
        agent đọc được); giá trị key KHÔNG đưa vào message lỗi."""
        try:
            key = self._secrets.get(self._key_name)
        except SecretNotFound as e:
            raise UserFacingError(
                f"secret '{self._key_name}' chưa đặt — đặt qua secret store/env "
                f"trước khi dùng web_search"
            ) from e
        if not isinstance(key, str) or not key:
            raise UserFacingError(
                f"secret '{self._key_name}' rỗng — cấu hình lại search API key"
            )
        return key

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        key = self._resolve_key()  # lấy tại điểm dùng, không log
        results = await self._search(args["query"], key)
        limit = int(args.get("limit", 5))
        kept: list[dict] = []
        blocked = 0
        for r in results:
            url = str(r.get("url") or "")
            if self._allowed_url(url):
                kept.append(r)
            else:
                blocked += 1
            if len(kept) >= limit:
                break
        lines = [
            f"- {r.get('title', '')}\n  {r.get('url', '')}\n  {r.get('snippet', '')}"
            for r in kept
        ]
        body = "\n".join(lines) if lines else "(không có kết quả trong egress allowlist)"
        if blocked and not lines:
            body += f"\n[DENIED] {blocked} kết quả ngoài egress allowlist đã bị lọc"
        elif blocked:
            body += f"\n({blocked} kết quả ngoài allowlist đã bị lọc)"
        span_attrs: dict[str, object] = {
            "provider": self._cost_provider,
            "model": self._cost_model,
            "queries": 1,
            "results_kept": len(kept),
            "results_blocked": blocked,
        }
        if self._cost_usd:
            span_attrs["cost_usd"] = self._cost_usd
        return ToolResult.success(body, span_attrs=span_attrs)
