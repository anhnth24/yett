"""RPC code execution broker (spec P2 §8, WP2.5). Viết lại từ design Hermes (KHÔNG vendor code).

Agent viết script gọi tool qua RPC → gom pipeline nhiều bước thành một lượt. MỖI RPC call
vẫn xuyên Policy Gate (không leo thang quyền). Caps: timeout, số call, kích thước output.
Fix theo Hermes issues #41/#7071: strip secret khỏi env, không recursive execute_code.

Bản này: broker in-process (transport injectable). Bản production chạy child trong container
với Unix socket — cùng interface CallHandler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from yett.tools.base import ToolResult

# Handler xử lý một RPC call: (tool, args) -> ToolResult. CHÍNH LÀ execute_tool đã bọc Gate.
CallHandler = Callable[[str, dict], Awaitable[ToolResult]]


@dataclass
class RpcCaps:
    max_calls: int = 50
    max_output_bytes: int = 50_000


@dataclass
class RpcSession:
    handler: CallHandler
    caps: RpcCaps = field(default_factory=RpcCaps)
    # [P1-10/RT-8] Toolset con subagent — THAM SỐ tường minh (giống `execute_tool`), không
    # duck-type qua ctx nào cả. None = không giới hạn (phiên RPC cấp cha/không phải subagent).
    # Đây là phòng thủ Ở BROKER, ĐỘC LẬP với việc `handler` (thường là `execute_tool` đã bind
    # sẵn allowed_tools qua closure) có tự enforce hay không — subagent gọi RPC không được
    # vượt toolset con dù `handler` phía trên có quên truyền allowed_tools hay không.
    allowed_tools: set[str] | None = None
    calls_made: int = 0
    denied_tools: set[str] = field(default_factory=set)

    async def call(self, tool: str, args: dict) -> ToolResult:
        # cấm recursive execute_code (fix #7071) + cap số call
        if tool in ("execute_code", "delegate"):
            return ToolResult.error("[DENIED] không được gọi execute_code/delegate từ trong RPC")
        if self.allowed_tools is not None and tool not in self.allowed_tools:
            self.denied_tools.add(tool)
            return ToolResult.error(f"[DENIED] tool '{tool}' ngoài toolset con được phép (RPC)")
        if self.calls_made >= self.caps.max_calls:
            return ToolResult.error(f"[DENIED] vượt giới hạn {self.caps.max_calls} RPC calls")
        self.calls_made += 1
        result = await self.handler(tool, args)  # xuyên Policy Gate ở handler
        if result.is_error and "DENIED" in result.content:
            self.denied_tools.add(tool)
        # cap output
        if len(result.content) > self.caps.max_output_bytes:
            return ToolResult(
                ok=result.ok,
                content=result.content[: self.caps.max_output_bytes] + "\n[...cắt bớt output]",
                is_error=result.is_error,
            )
        return result


def strip_secret_env(env: dict[str, str]) -> dict[str, str]:
    """Loại mọi biến có mùi secret khỏi env của child (fix #41 PYTHONPATH/env leak)."""
    out = {}
    for k, v in env.items():
        ku = k.upper()
        if any(s in ku for s in ("SECRET", "TOKEN", "KEY", "PASSWORD", "PWD", "DSN", "PYTHONPATH")):
            continue
        out[k] = v
    return out
