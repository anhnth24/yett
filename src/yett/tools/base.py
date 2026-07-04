"""Tool interface + kết quả (spec P0-P1 §3.4).

Mọi tool khai báo JSON schema + validate. Lỗi validate là UserFacingError
(agent đọc được, sửa được). Tool thực thi qua wiring: Gate → Registry → Sandbox → Filters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    is_error: bool = False

    @staticmethod
    def success(content: str) -> "ToolResult":
        return ToolResult(ok=True, content=content, is_error=False)

    @staticmethod
    def error(reason: str) -> "ToolResult":
        # Trả lỗi dạng agent-đọc-được thay vì raise — để agent tự sửa và thử lại.
        return ToolResult(ok=False, content=reason, is_error=True)


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
