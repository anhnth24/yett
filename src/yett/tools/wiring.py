"""Tool execution wiring (spec P0-P1 §4). Bất biến: Gate → (approval) → validate → run → Filters.

Đây là ĐIỂM DUY NHẤT tool được thực thi. Core loop gọi execute_tool(), không gọi
sandbox/registry trực tiếp (import-linter enforce). Mọi quyết định Gate được audit.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from yett.errors import UserFacingError
from yett.security.filters import apply as filter_apply
from yett.security.gate import Decision, PolicyGate, SessionCtx, safe_evaluate
from yett.tools.base import ToolResult
from yett.tools.registry import Registry

# Approver: async callable trả True nếu người dùng duyệt (timeout → False).
Approver = Callable[[str, dict], Awaitable[bool]]
# Auditor: ghi lại quyết định (gate verdict, approval...).
Auditor = Callable[..., None]

# Tool có kết quả từ nguồn ngoài → cần scan injection.
_UNTRUSTED_TOOLS = {"web_fetch", "web_search", "read_file", "log_read", "ssh_exec"}


async def execute_tool(
    name: str,
    args: dict,
    ctx: SessionCtx,
    *,
    gate: PolicyGate,
    registry: Registry,
    approver: Approver | None = None,
    auditor: Auditor | None = None,
) -> ToolResult:
    dec: Decision = safe_evaluate(gate, name, args, ctx)
    if auditor:
        auditor(kind="gate", tool=name, verdict=dec.verdict, rule_id=dec.rule_id)

    if dec.verdict == "deny":
        # Trả lỗi agent-đọc-được thay vì raise — agent tự sửa và thử lại.
        return ToolResult.error(f"[DENIED] {dec.reason}")

    if dec.verdict == "need_approval":
        approved = await approver(name, args) if approver else False
        if auditor:
            auditor(kind="approval", tool=name, approved=approved)
        if not approved:
            return ToolResult.error("[DENIED] approval bị từ chối hoặc hết hạn")

    tool = registry.get(name)
    try:
        tool.validate(args)
        raw = await tool.run(args, ctx)
    except UserFacingError as e:
        # Lỗi agent-đọc-được (sai schema, path ngoài scope...) → trả về để agent tự sửa.
        return ToolResult.error(str(e))
    return filter_apply(raw, untrusted=name in _UNTRUSTED_TOOLS)
