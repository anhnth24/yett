"""exec tool (spec P0-P1). Chạy lệnh trong sandbox. Gate đã chạy trước ở wiring."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable

from yett.errors import UserFacingError
from yett.sandbox.base import Sandbox
from yett.tools.base import ToolCtx, ToolResult
from yett.tools.projects import ProjectScope


class ExecTool:
    """Chạy một lệnh shell trong sandbox.

    `cwd` (nếu có) được scope-check trên path HOST qua `ProjectScope` TRƯỚC, rồi map sang
    path sandbox (container, khi backend=docker) qua `to_sandbox_path`. Scope-check này chỉ
    là vệ sinh (đường dẫn hợp lệ trong workspace/project đã đăng ký) — KHÔNG phải containment
    thật: thân lệnh (`sh -c <cmd>`) vẫn có thể `cd` hoặc dùng path tuyệt đối để thoát ra ngoài.
    Containment thật đến từ cách ly Docker (mount + `--network none` + rootfs read-only);
    trên LocalSandbox (dev/test), exec KHÔNG được coi là an toàn với lệnh không tin cậy.
    """

    name = "exec"
    schema = {
        "type": "object",
        "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}},
        "required": ["cmd"],
    }

    def __init__(
        self,
        sandbox: Sandbox,
        timeout: int = 120,
        *,
        scope: ProjectScope | None = None,
        to_sandbox_path: Callable[[Path], str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._scope = scope
        self._to_sandbox_path = to_sandbox_path

    def validate(self, args: dict) -> None:
        if not args.get("cmd"):
            raise UserFacingError("thiếu 'cmd'")
        try:
            shlex.split(args["cmd"])
        except ValueError as e:
            raise UserFacingError(f"cmd không parse được: {e}")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        argv = ["sh", "-c", args["cmd"]]
        sandbox_cwd = self._resolve_cwd(args.get("cwd"))
        res = await self._sandbox.run(argv, cwd=sandbox_cwd, timeout=self._timeout)
        body = f"exit={res.exit_code}\n--- stdout ---\n{res.stdout}"
        if res.stderr:
            body += f"\n--- stderr ---\n{res.stderr}"
        return ToolResult(ok=res.exit_code == 0, content=body, is_error=res.exit_code != 0)

    def _resolve_cwd(self, cwd: str | None) -> str | None:
        """Scope-check `cwd` (host) rồi map sang path sandbox. Ngoài scope → UserFacingError
        (agent đọc được, tự sửa). Không có `scope`/`to_sandbox_path` (vd test cũ) → giữ
        hành vi cũ: truyền thẳng chuỗi `cwd`."""
        if not cwd:
            return None
        host_path = self._scope.resolve_in_scope(cwd) if self._scope is not None else Path(cwd)
        if self._to_sandbox_path is not None:
            return self._to_sandbox_path(host_path)
        return str(host_path) if self._scope is not None else cwd
