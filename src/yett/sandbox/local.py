"""LocalSandbox — chạy lệnh bằng subprocess (dev/test), cross-platform.

CẢNH BÁO: không cô lập như Docker. Production dùng DockerSandbox; LocalSandbox chỉ
cho dev/test và bị Policy Gate chặn trong build production (config `sandbox.backend`).
Trong môi trường không có Docker daemon (vd CI của repo), LocalSandbox cho phép test
được toàn bộ wiring Gate→Registry→Sandbox→Filters mà không cần daemon.

Cross-platform: nếu caller truyền lệnh dạng ["sh","-c", cmd] mà máy là Windows và
không có sh trong PATH, tự chuyển sang ["cmd","/c", cmd]. cmdguard đã chặn cả lệnh
xóa của Windows (del/rd/Remove-Item) nên hardline vẫn phủ.
"""

from __future__ import annotations

import asyncio
import shutil
import sys

from yett.sandbox.base import ExecResult


def _adapt_shell(cmd: list[str]) -> list[str]:
    """Trên Windows không có sh → đổi ['sh','-c', X] thành shell Windows phù hợp."""
    if sys.platform != "win32":
        return cmd
    if len(cmd) >= 3 and cmd[0] in ("sh", "bash") and cmd[1] == "-c":
        if shutil.which("sh"):  # Git-for-Windows cung cấp sh → giữ nguyên
            return cmd
        return ["cmd", "/c", cmd[2]]
    return cmd


class LocalSandbox:
    async def run(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int = 120, env: dict | None = None
    ) -> ExecResult:
        cmd = _adapt_shell(cmd)
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
