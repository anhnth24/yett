"""Tool interface + kết quả (spec P0-P1 §3.4).

Mọi tool khai báo JSON schema + validate. Lỗi validate là UserFacingError
(agent đọc được, sửa được). Tool thực thi qua wiring: Gate → Registry → Sandbox → Filters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    is_error: bool = False
    # Attrs gắn vào TOOL_CALL span (cost ledger...). KHÔNG chứa secret / plaintext key.
    span_attrs: dict[str, object] = field(default_factory=dict)

    @staticmethod
    def success(content: str, *, span_attrs: dict[str, object] | None = None) -> "ToolResult":
        return ToolResult(
            ok=True, content=content, is_error=False, span_attrs=dict(span_attrs or {})
        )

    @staticmethod
    def error(reason: str, *, span_attrs: dict[str, object] | None = None) -> "ToolResult":
        # Trả lỗi dạng agent-đọc-được thay vì raise — để agent tự sửa và thử lại.
        return ToolResult(
            ok=False, content=reason, is_error=True, span_attrs=dict(span_attrs or {})
        )


class ToolCtx(Protocol):
    """Ngữ cảnh cấp cho tool khi run (đầy đủ hoá khi làm core)."""

    session_key: str


class Tool(Protocol):
    name: str
    schema: dict  # JSON schema cho args

    def validate(self, args: dict) -> None:
        """Raise UserFacingError nếu args sai schema."""
        ...

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult: ...
