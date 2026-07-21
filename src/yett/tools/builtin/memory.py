"""memory_propose — agent đề xuất ghi MEMORY.md vào staging (Memory Review Gate).

Bất biến (RG1-8): tool này KHÔNG ghi MEMORY.md. Chỉ gọi MemoryReviewGate.propose()
→ memory/pending/<id>.md. Người vận hành duyệt qua CLI (`yett memory approve`).

Giới hạn:
- Chỉ session chính (`session_key == "main"`) — MEMORY.md là main-session-only.
- Subagent không được đề xuất (không leo thang / không ghi chéo session).
- Secret trong nội dung bị redact trước khi vào staging (không để lộ lên đĩa/context).
"""

from __future__ import annotations

from yett.errors import UserFacingError
from yett.memory.review_gate import MemoryReviewGate
from yett.security.filters import redact
from yett.tools.base import ToolCtx, ToolResult

_MAIN_SESSION = "main"


class MemoryProposeTool:
    """Đề xuất một mẩu ghi nhớ dài hạn vào staging (chờ người duyệt merge vào MEMORY.md)."""

    name = "memory_propose"
    schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "mẩu memory đề xuất (sự kiện/ưu tiên/thói quen đáng nhớ lâu dài)",
            },
        },
        "required": ["content"],
    }

    def __init__(self, gate: MemoryReviewGate) -> None:
        self._gate = gate

    def validate(self, args: dict) -> None:
        if not (args.get("content") or "").strip():
            raise UserFacingError("thiếu 'content' (nội dung đề xuất memory)")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        if getattr(ctx, "is_subagent", False):
            return ToolResult.error(
                "[DENIED] subagent không được đề xuất MEMORY.md (chỉ session chính)"
            )
        if getattr(ctx, "session_key", None) != _MAIN_SESSION:
            return ToolResult.error(
                "[DENIED] memory_propose chỉ dùng ở session 'main' "
                f"(hiện tại: {getattr(ctx, 'session_key', '?')})"
            )
        # Redact secret TRƯỚC khi ghi staging — không để credential nằm trên đĩa chờ duyệt.
        content = redact(str(args["content"]))
        try:
            pid = self._gate.propose(content)
        except ValueError as e:
            return ToolResult.error(str(e))
        return ToolResult.success(
            f"Đã đề xuất memory id={pid} vào staging "
            f"(chưa ghi MEMORY.md — chờ `yett memory approve {pid}`).",
            span_attrs={"memory_action": "proposed", "proposal_id": pid},
        )
