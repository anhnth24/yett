"""notify tool: agent chủ động đẩy tin cho người dùng (Telegram/kênh đã bật).

'Deploy xong rồi báo tôi' → agent gọi notify khi hoàn tất. Kênh do App.set_notifier gắn
(vd TelegramChannel.notify). Chưa bật kênh → trả lỗi agent-đọc-được (không phải sự cố).
"""

from __future__ import annotations

from typing import Callable

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult


class NotifyTool:
    """Gửi một thông báo ngắn cho người dùng qua kênh ngoài (vd Telegram)."""

    name = "notify"
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "nội dung thông báo"}},
        "required": ["text"],
    }

    def __init__(self, notifier_getter: Callable[[], Callable[[str], int] | None]) -> None:
        # getter → hàm notify hiện tại (hoặc None nếu chưa bật kênh). Getter để hot-reload/late-bind.
        self._get = notifier_getter

    def validate(self, args: dict) -> None:
        if not (args.get("text") or "").strip():
            raise UserFacingError("thiếu 'text'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        notify = self._get()
        if notify is None:
            return ToolResult.error("chưa bật kênh thông báo (Telegram). Bật trong config để dùng notify.")
        n = notify(args["text"])
        return ToolResult.success(f"Đã gửi thông báo tới {n} kênh.")
