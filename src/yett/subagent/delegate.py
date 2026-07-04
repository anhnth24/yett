"""delegate tool — subagent 1 cấp (spec P3 §5.2).

Bất biến: CÙNG PolicyGate + policy (không leo thang quyền); toolset con ⊆ cha;
KHÔNG delegate lồng nhau (chặn ở đây); span lồng dưới cha.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from yett.errors import DenyError, UserFacingError
from yett.subagent.definition import load_subagent
from yett.tools.base import ToolCtx, ToolResult


@dataclass
class DelegateCtx:
    """Ngữ cảnh cha truyền cho delegate."""

    session_key: str
    allowed_tools: set[str]
    is_subagent: bool = False
    parent_span_id: str | None = None


# runner: chạy một subagent turn, trả text. Nhận (def, ctx con) → text.
SubagentRunner = Callable[..., Awaitable[str]]


class DelegateTool:
    """Ủy quyền một task cho subagent (1 cấp, không leo thang quyền)."""

    name = "delegate"
    schema = {
        "type": "object",
        "properties": {"agent": {"type": "string"}, "task": {"type": "string"}},
        "required": ["agent", "task"],
    }

    def __init__(self, agents_dir: Path, runner: SubagentRunner) -> None:
        self._dir = agents_dir
        self._runner = runner

    def validate(self, args: dict) -> None:
        if not args.get("agent") or not args.get("task"):
            raise UserFacingError("cần 'agent' và 'task'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        dctx: DelegateCtx = ctx  # type: ignore[assignment]
        # KHÔNG delegate lồng nhau (1 cấp)
        if getattr(dctx, "is_subagent", False):
            raise DenyError("delegate lồng nhau bị cấm (chỉ 1 cấp)", "DELEGATE_NESTED")
        # load + kiểm toolset ⊆ cha (không leo thang quyền)
        sub = load_subagent(self._dir, args["agent"], parent_toolset=dctx.allowed_tools)
        child = DelegateCtx(
            session_key=f"{dctx.session_key}:sub:{sub.name}",
            allowed_tools=set(sub.toolset),
            is_subagent=True,
            parent_span_id=dctx.parent_span_id,
        )
        text = await self._runner(sub, child, args["task"])
        return ToolResult.success(text)
