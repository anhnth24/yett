"""Task tools (lớp trợ lý): agent tự quản lý việc cần làm của người dùng.

'nhắc tôi deploy UAT mai' → task_add; 'tôi đang làm gì' → task_list. Việc ghi vào TaskStore
của yett (state riêng), KHÔNG đụng file/DB người dùng → rủi ro thấp; vẫn qua Policy Gate như
mọi tool (allowlist trong config). due dạng ISO 'YYYY-MM-DD'.
"""

from __future__ import annotations

from yett.errors import UserFacingError
from yett.memory.tasks import TaskStore
from yett.tools.base import ToolCtx, ToolResult


def _fmt(t) -> str:
    bits = [f"#{t.id} [{t.status}]", t.title]
    if t.priority != "normal":
        bits.append(f"({t.priority})")
    if t.project:
        bits.append(f"@{t.project}")
    if t.due:
        bits.append(f"due {t.due}")
    return " ".join(bits)


class TaskAddTool:
    """Thêm một việc cần làm / mục tiêu cho người dùng."""

    name = "task_add"
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "nội dung việc"},
            "project": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "due": {"type": "string", "description": "hạn ISO 'YYYY-MM-DD'"},
            "notes": {"type": "string"},
        },
        "required": ["title"],
    }

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def validate(self, args: dict) -> None:
        if not (args.get("title") or "").strip():
            raise UserFacingError("thiếu 'title'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        try:
            t = self._store.add(
                args["title"], project=args.get("project"),
                priority=args.get("priority", "normal"),
                due=args.get("due"), notes=args.get("notes"),
            )
        except ValueError as e:
            return ToolResult.error(str(e))
        return ToolResult.success(f"Đã thêm việc: {_fmt(t)}")


class TaskListTool:
    """Liệt kê việc cần làm (lọc theo status/project)."""

    name = "task_list"
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["todo", "doing", "done"]},
            "project": {"type": "string"},
        },
    }

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def validate(self, args: dict) -> None:
        return None

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        tasks = self._store.list_tasks(status=args.get("status"), project=args.get("project"))
        if not tasks:
            return ToolResult.success("(không có việc nào khớp)")
        return ToolResult.success("\n".join(_fmt(t) for t in tasks))


class TaskUpdateTool:
    """Cập nhật việc (đổi status/title/priority/due/notes). status='done' để hoàn thành."""

    name = "task_update"
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": ["todo", "doing", "done"]},
            "title": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "due": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["id"],
    }

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def validate(self, args: dict) -> None:
        if not isinstance(args.get("id"), int):
            raise UserFacingError("thiếu 'id' (số nguyên)")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        if self._store.get(args["id"]) is None:
            return ToolResult.error(f"không có việc #{args['id']}")
        try:
            t = self._store.update(
                args["id"], status=args.get("status"), title=args.get("title"),
                priority=args.get("priority"), due=args.get("due"), notes=args.get("notes"),
            )
        except ValueError as e:
            return ToolResult.error(str(e))
        assert t is not None
        return ToolResult.success(f"Đã cập nhật: {_fmt(t)}")
