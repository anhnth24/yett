"""Tool Registry (spec P0-P1 §3.4). Đăng ký tool, cấp schema cho provider, validate."""

from __future__ import annotations

from yett.errors import UserFacingError
from yett.provider.base import ToolSchema
from yett.tools.base import Tool


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool trùng tên: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise UserFacingError(f"tool '{name}' không tồn tại")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, allowed: set[str] | None = None) -> list[ToolSchema]:
        """Schema cho provider. allowed=None → tất cả; ngược lại lọc theo toolset (subagent P3)."""
        out = []
        for name, tool in sorted(self._tools.items()):
            if allowed is not None and name not in allowed:
                continue
            out.append(ToolSchema(name=name, description=_desc(tool), parameters=tool.schema))
        return out


def _desc(tool: Tool) -> str:
    return (tool.__doc__ or "").strip().split("\n")[0] or tool.name
