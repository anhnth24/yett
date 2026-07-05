"""LocalSandbox — chạy lệnh bằng subprocess trực tiếp trên host (CHỈ dev/test), cross-platform.

CẢNH BÁO: KHÔNG cô lập — không network isolation, không filesystem containment, không giới
hạn tài nguyên; lệnh chạy thẳng trên máy host. CHỈ dùng khi config khai `sandbox.backend:
local` (mặc định production là `docker`). App tự chọn LocalSandbox khi cfg khai backend=local;
không có Policy Gate hay cơ chế runtime nào khác "hạ cấp" từ Docker về Local — nếu backend
là `docker` mà Docker/daemon thiếu, App fail-closed (từ chối khởi động), KHÔNG rơi về
LocalSandbox (xem `yett.sandbox.docker.probe_docker`, `App.__init__`).

Trong môi trường không có Docker daemon (vd máy dev, CI của repo), LocalSandbox cho phép test
được toàn bộ wiring Gate→Registry→Sandbox→Filters mà không cần daemon — nhưng bản thân exec
qua LocalSandbox KHÔNG được coi là an toàn để chạy lệnh không tin cậy.

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
