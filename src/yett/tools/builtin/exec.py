"""exec tool (spec P0-P1). Chạy lệnh trong sandbox. Gate đã chạy trước ở wiring."""

from __future__ import annotations

import shlex

from yett.errors import UserFacingError
from yett.sandbox.base import Sandbox
from yett.tools.base import ToolCtx, ToolResult


class ExecTool:
    """Chạy một lệnh shell trong sandbox cô lập."""

    name = "exec"
    schema = {
        "type": "object",
        "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}},
        "required": ["cmd"],
    }

    def __init__(self, sandbox: Sandbox, timeout: int = 120) -> None:
        self._sandbox = sandbox
        self._timeout = timeout

    def validate(self, args: dict) -> None:
        if not args.get("cmd"):
            raise UserFacingError("thiếu 'cmd'")
        try:
            shlex.split(args["cmd"])
        except ValueError as e:
            raise UserFacingError(f"cmd không parse được: {e}")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        argv = ["sh", "-c", args["cmd"]]
        res = await self._sandbox.run(argv, cwd=args.get("cwd"), timeout=self._timeout)
        body = f"exit={res.exit_code}\n--- stdout ---\n{res.stdout}"
        if res.stderr:
            body += f"\n--- stderr ---\n{res.stderr}"
        return ToolResult(ok=res.exit_code == 0, content=body, is_error=res.exit_code != 0)
