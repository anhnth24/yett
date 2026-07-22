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


def _resolve_db_driver_hint(registry: Registry, args: dict) -> str | None:
    """[Phase 6 handoff từ Phase 4] Gate chạy TRƯỚC tool.run() nên chưa biết driver thật của
    DB profile (`args['profile']` mới chỉ là tên, chưa resolve). Đọc driver best-effort TỪ
    CHÍNH `DbQueryTool` đã đăng ký trong registry — không I/O, không đụng secret, không gọi
    `tool.run()`. `tools/db/` không thuộc phạm vi sửa của phase này nên không thêm accessor
    công khai được; dùng getattr phòng thủ, bất kỳ shape lạ nào cũng trả None (không raise) để
    `ImmutableCore` tự fallback fail-closed multi-dialect như trước (xem `immutable.py`)."""
    if not registry.has("db_query"):
        return None
    profile_name = args.get("profile")
    if not isinstance(profile_name, str) or not profile_name:
        return None
    profiles = getattr(registry.get("db_query"), "_profiles", None)
    if not isinstance(profiles, dict):
        return None
    driver = getattr(profiles.get(profile_name), "driver", None)
    return driver if isinstance(driver, str) else None


def _gate_args(name: str, args: dict, registry: Registry) -> dict:
    """Bản args Gate thấy — có thể được làm giàu (vd driver hint cho db_query) so với args
    THẬT truyền cho tool.run(). Không đổi `args` gốc (tool tự resolve profile/driver riêng,
    đã đúng từ Phase 4)."""
    if name != "db_query":
        return args
    driver = _resolve_db_driver_hint(registry, args)
    return {**args, "driver": driver} if driver is not None else args


async def execute_tool(
    name: str,
    args: dict,
    ctx: SessionCtx,
    *,
    gate: PolicyGate,
    registry: Registry,
    approver: Approver | None = None,
    auditor: Auditor | None = None,
    hooks=None,  # HookRunner | None — chạy Pre/PostToolUse (mutate/deny)
    allowed_tools: set[str] | None = None,
) -> ToolResult:
    # [P1-10/RT-8] Toolset con subagent — THAM SỐ tường minh, không duck-type qua ctx
    # (SessionCtx Protocol/_SimpleCtx không có allowed_tools, tra qua đó sẽ luôn-sai/raise).
    # None = không giới hạn (cấp cha). Kiểm TRƯỚC Gate vì đây là biên cấu trúc (toolset con ⊆
    # cha), không phải một quyết định policy.
    if allowed_tools is not None and name not in allowed_tools:
        if auditor:
            auditor(kind="gate", tool=name, verdict="deny", rule_id="TOOLSET_SUBSET")
        return ToolResult.error(f"[DENIED] tool '{name}' ngoài toolset con được phép")

    dec: Decision = safe_evaluate(gate, name, _gate_args(name, args, registry), ctx)
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

    # PreToolUse hooks — sau Gate (không nới được deny của Gate), có thể mutate args hoặc deny.
    if hooks is not None:
        from yett.hooks.runner import HookEvent

        pre = await hooks.run(HookEvent("PreToolUse", name, args))
        if pre.action == "deny":
            return ToolResult.error(f"[DENIED] hook: {pre.reason}")

        # [P1-11] Hook chạy SAU Gate và có quyền mutate args — verdict `dec` (và approval đã
        # cấp cho args gốc) chỉ còn đáng tin khi args KHÔNG đổi. Nếu hook MUTATE args (vd đổi
        # `echo safe` → `rm -rf /`) thì phải re-gate trên args mới, xử lý verdict lần 2 ĐẦY ĐỦ:
        # deny → chặn ngay; need_approval → hỏi duyệt LẠI với args mới (KHÔNG tái dùng `approved`
        # ở trên — approval đó cấp cho args gốc); approver vắng → deny fail-closed.
        # Hook KHÔNG mutate (kể cả HookRunner rỗng) → giữ nguyên quyết định gốc, KHÔNG hỏi duyệt
        # lần hai (nếu re-gate vô điều kiện, tool need_approval sẽ bị hỏi duyệt 2 lần).
        if pre.mutated_args is not None:
            args = pre.mutated_args
            redec = safe_evaluate(gate, name, _gate_args(name, args, registry), ctx)
            if auditor:
                auditor(kind="gate", tool=name, verdict=redec.verdict, rule_id=redec.rule_id, phase="post_hook")
            if redec.verdict == "deny":
                return ToolResult.error(f"[DENIED] {redec.reason}")
            if redec.verdict == "need_approval":
                reapproved = await approver(name, args) if approver else False
                if auditor:
                    auditor(kind="approval", tool=name, approved=reapproved, phase="post_hook")
                if not reapproved:
                    return ToolResult.error("[DENIED] approval bị từ chối hoặc hết hạn")

    try:
        # Registry lookup is part of the agent-readable execution boundary.  A provider can
        # still emit a stale/hallucinated tool name when configuration changed after a
        # checkpoint, or when an allowlist rule exists for an optional tool that was not
        # wired.  Do not let that abort the turn after Gate already allowed it.
        tool = registry.get(name)
        tool.validate(args)
        raw = await tool.run(args, ctx)
    except UserFacingError as e:
        # Lỗi agent-đọc-được (sai schema, path ngoài scope...) → trả về để agent tự sửa.
        return ToolResult.error(str(e))

    result = filter_apply(raw, untrusted=name in _UNTRUSTED_TOOLS)

    # PostToolUse hooks — có thể mutate result.
    if hooks is not None:
        from yett.hooks.runner import HookEvent

        post = await hooks.run(HookEvent("PostToolUse", name, args, result.content))
        mutated = post.mutated_result if post.mutated_result is not None else result.content
        # [P1-16] Hook chạy SAU Filters — nội dung hook chèn/sửa (vd secret injected sau khi
        # đã redact) CHƯA từng qua redact/injection-scan. Lọc LẠI trước khi trả về (context sẽ
        # checkpoint chuỗi này) thay vì trả thẳng `mutated_result` như trước.
        result = filter_apply(
            ToolResult(
                ok=result.ok,
                content=mutated,
                is_error=result.is_error,
                span_attrs=dict(result.span_attrs),
            ),
            untrusted=name in _UNTRUSTED_TOOLS,
        )
    return result
