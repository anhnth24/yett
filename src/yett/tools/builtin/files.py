"""read_file / write_file (spec P0-P1). Giới hạn trong project scope; chống path-traversal."""

from __future__ import annotations


from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult
from yett.tools.projects import ProjectScope


class ReadFileTool:
    """Đọc nội dung một file trong workspace/project đã đăng ký."""

    name = "read_file"
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, scope: ProjectScope) -> None:
        self._scope = scope

    def validate(self, args: dict) -> None:
        if not args.get("path"):
            raise UserFacingError("thiếu 'path'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        p = self._scope.resolve_in_scope(args["path"])  # raise nếu ngoài scope
        if not p.exists():
            return ToolResult.error(f"file không tồn tại: {args['path']}")
        return ToolResult.success(p.read_text(encoding="utf-8", errors="replace"))


class WriteFileTool:
    """Ghi nội dung vào một file trong workspace/project đã đăng ký."""

    name = "write_file"
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self, scope: ProjectScope) -> None:
        self._scope = scope

    def validate(self, args: dict) -> None:
        if not args.get("path"):
            raise UserFacingError("thiếu 'path'")
        if "content" not in args:
            raise UserFacingError("thiếu 'content'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        p = self._scope.resolve_in_scope(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return ToolResult.success(f"đã ghi {len(args['content'])} ký tự vào {args['path']}")
