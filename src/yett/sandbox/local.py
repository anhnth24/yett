"""LocalSandbox — chạy lệnh bằng subprocess (dev/test).

CẢNH BÁO: không cô lập như Docker. Production dùng DockerSandbox; LocalSandbox chỉ
cho dev/test và bị Policy Gate chặn trong build production (config `sandbox.backend`).
Trong môi trường không có Docker daemon (vd CI của repo), LocalSandbox cho phép test
được toàn bộ wiring Gate→Registry→Sandbox→Filters mà không cần daemon.
"""

from __future__ import annotations

import asyncio

from yett.sandbox.base import ExecResult


class LocalSandbox:
    async def run(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int = 120, env: dict | None = None
    ) -> ExecResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            return ExecResult("", f"lệnh không tồn tại: {e}", 127)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult("", f"timeout sau {timeout}s", 124, timed_out=True)
        return ExecResult(
            out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
        )

    async def cleanup(self) -> None:
        return None
