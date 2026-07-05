"""load_skill tool (spec P2 §4). Progressive disclosure: agent gọi để nạp body SKILL.md khi cần."""

from __future__ import annotations

from yett.errors import UserFacingError
from yett.skills.loader import SkillLoader
from yett.tools.base import ToolCtx, ToolResult


class LoadSkillTool:
    """Nạp hướng dẫn chi tiết của một skill (theo tên trong menu) khi cần dùng."""

    name = "load_skill"
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def validate(self, args: dict) -> None:
        if not args.get("name"):
            raise UserFacingError("thiếu 'name' skill")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        try:
            body = self._loader.load_body(args["name"])
        except UserFacingError as e:
            return ToolResult.error(str(e))
        return ToolResult.success(body)
