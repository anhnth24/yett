"""Agent loop (spec P0-P1 §4, WP1.2).

ContextStage → [Think → Prune → Tool → Observe → Checkpoint]×N → Finalize.
Bất biến: bounded iterations; mỗi tool call qua wiring (Gate→...→Filters); checkpoint
để resume không lặp side-effect; cancel sạch ở ranh giới stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from yett.core.cancel import Cancelled, CancelToken
from yett.core.checkpoint import deserialize_messages, serialize_messages
from yett.core.context import Context, pending_tool_calls, tool_call_signature
from yett.obs.tracer import SpanKind
from yett.provider.base import ChatResult, ToolCall
from yett.provider.failover import ContextOverflow, FailoverRouter
from yett.security.gate import PolicyGate
from yett.tools.registry import Registry
from yett.tools.wiring import Approver, Auditor, execute_tool

# clock injectable để test deterministic (spec §0).
Clock = Callable[[], float]

# RT-3: cả "running" (crash giữa turn) và "canceled" (hủy sạch) đều resumable — chỉ trạng
# thái "done" mới coi turn đã xong, không cần (và không nên) khôi phục lại.
_RESUMABLE_STATUSES = ("running", "canceled")


@dataclass
class LoopConfig:
    max_iterations: int = 20
    context_token_budget: int = 150_000
    # Anti-loop (học từ knowledge-agent-template §policy): giữ N bước cuối để bắt buộc
    # trả lời (ép text-only, bỏ tool); và ép trả lời khi một tool-call bị lặp lại quá nhiều.
    reserve_final_steps: int = 1
    force_text_after_repeats: int = 3


@dataclass
class TurnResult:
    text: str
    iterations: int
    status: str  # "done" | "max_iterations" | "canceled"
    trace_id: str
    completed_tool_calls: list[str] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        router: FailoverRouter,
        gate: PolicyGate,
        registry: Registry,
        tracer,
        checkpoints,
        *,
        cfg: LoopConfig | None = None,
        clock: Clock,
        approver: Approver | None = None,
        auditor: Auditor | None = None,
        cost_fn: Callable[[ChatResult], float] | None = None,
        hooks=None,
    ) -> None:
        self._router = router
        self._gate = gate
        self._registry = registry
        self._tracer = tracer
        self._ckpt = checkpoints
        self._cfg = cfg or LoopConfig()
        self._clock = clock
        self._approver = approver
        self._auditor = auditor
        self._cost_fn = cost_fn or (lambda r: 0.0)
        self._hooks = hooks

    async def run_turn(
        self,
        ctx: Context,
        *,
        session_key: str,
        turn_id: str,
        cancel: CancelToken | None = None,
        session_ctx=None,
        allowed_tools: set[str] | None = None,
        max_iterations: int | None = None,
    ) -> TurnResult:
        cancel = cancel or CancelToken()
        session_ctx = session_ctx or _SimpleCtx(session_key)
        # Router có thể siết số vòng theo độ khó câu hỏi; luôn bị chặn trên bởi cfg.
        eff_max = self._cfg.max_iterations
        if max_iterations is not None:
            eff_max = max(1, min(max_iterations, self._cfg.max_iterations))
        root = self._tracer.start_span(
            SpanKind.AGENT, "turn", start_ts=self._clock(), session_key=session_key
        )
        completed: list[str] = []  # id trong PHẠM VI lần chạy này (trả về TurnResult)
        # RT-2: chữ ký ổn định -> kết quả + trạng thái lỗi (idempotency).
        # Giá trị string cũ vẫn được đọc khi resume checkpoint từ phiên bản trước.
        completed_sigs: dict[str, object] = {}

        # RT-2/RT-3: resume — "running" (crash giữa turn) HOẶC "canceled" (hủy sạch) đều
        # khôi phục được: nạp lại TOÀN BỘ lịch sử message (không chỉ id) để loop không phải
        # hỏi lại model từ đầu; tránh lặp side-effect đã có.
        prev = self._ckpt.load(session_key, turn_id)
        is_resume = prev is not None and prev["status"] in _RESUMABLE_STATUSES
        if is_resume:
            state = prev["state"]
            history = state.get("history")
            if history is not None:
                ctx.messages = deserialize_messages(history)
            completed_sigs = dict(state.get("completed_sigs", {}))

        status = "done"
        text = ""
        i = 0
        call_counts: dict[str, int] = {}  # đếm tool-call lặp để chống loop
        try:
            if is_resume:
                # Batch dang dở (assistant tool_use chưa đủ tool_result đi kèm) → hoàn tất
                # TRƯỚC khi hỏi model tiếp — gửi request còn tool_call chưa trả lời là vi
                # phạm thứ tự OpenAI/Anthropic. Trong cùng try/except Cancelled bên dưới để
                # hủy giữa lúc hoàn tất batch dang dở cũng được xử lý sạch (RT-3).
                for tc in pending_tool_calls(ctx.messages):
                    cancel.check()
                    await self._execute_tool_call(
                        tc, ctx, completed, completed_sigs,
                        session_ctx=session_ctx, root=root, session_key=session_key,
                        turn_id=turn_id, iteration=prev["iteration"], consult_cache=True,
                        allowed_tools=allowed_tools,
                    )
            for i in range(eff_max):
                cancel.check()
                # Anti-loop: giữ bước cuối để bắt buộc trả lời (hết budget); hoặc ép trả lời
                # khi một tool-call bị lặp quá nhiều (agent kẹt).
                reserve_hit = i >= eff_max - self._cfg.reserve_final_steps
                repeat_hit = any(n >= self._cfg.force_text_after_repeats for n in call_counts.values())
                force_text = reserve_hit or repeat_hit
                # THINK
                span = self._tracer.start_span(
                    SpanKind.LLM_CALL, "think", start_ts=self._clock(),
                    trace_id=root.trace_id, parent_id=root.id, session_key=session_key,
                )
                tools = [] if force_text else self._registry.schemas(allowed_tools)
                try:
                    res = await self._router.chat(ctx.messages, tools)
                except ContextOverflow:
                    ctx.prune(self._cfg.context_token_budget // 2)
                    self._tracer.end_span(span, end_ts=self._clock(), event="context_overflow_pruned")
                    continue
                self._tracer.end_span(
                    span, end_ts=self._clock(),
                    provider=res.provider_name or self._router_name(), model=res.raw_model,
                    input_tokens=res.usage.input_tokens, output_tokens=res.usage.output_tokens,
                    cost_usd=self._cost_fn(res),
                )
                if force_text or res.stop_reason == "end_turn" or not res.tool_calls:
                    text = res.text or ""
                    ctx.add_assistant(text)
                    if force_text and res.tool_calls:
                        # Model vẫn muốn dùng tool nhưng bị ép trả lời.
                        if reserve_hit:
                            status = "max_iterations"  # cạn budget → câu trả lời best-effort
                            self._emit_event(root, "max_iterations_reached", session_key)
                        else:
                            self._emit_event(root, "forced_text_only", session_key)  # chống loop
                    break
                # P0-2: ghi lượt assistant tool_use vào context TRƯỚC khi chạy tool — đúng
                # thứ tự OpenAI/Anthropic (message tool phải đứng ngay sau assistant mang
                # tool_calls khớp id). force_text đã break ở nhánh trên nên không double-add.
                ctx.add_assistant_tool_calls(res.tool_calls, res.text)
                # PRUNE
                if ctx.tokens() > self._cfg.context_token_budget:
                    ctx.prune(self._cfg.context_token_budget)
                # TOOL + OBSERVE
                for tc in res.tool_calls:
                    cancel.check()
                    if tc.id in completed:
                        continue  # id còn ổn định trong CÙNG một lần chạy
                    sig = tool_call_signature(tc.name, tc.args)
                    call_counts[sig] = call_counts.get(sig, 0) + 1
                    await self._execute_tool_call(
                        tc, ctx, completed, completed_sigs,
                        session_ctx=session_ctx, root=root, session_key=session_key,
                        turn_id=turn_id, iteration=i, consult_cache=is_resume,
                        allowed_tools=allowed_tools,
                    )
            else:
                status = "max_iterations"
                self._emit_event(root, "max_iterations_reached", session_key)
        except Cancelled:
            status = "canceled"
            self._ckpt.mark(session_key, turn_id, "canceled", ts=self._clock())
            self._emit_event(root, "canceled", session_key)
            text = "Đã hủy."
            self._tracer.end_span(root, end_ts=self._clock(), status=status)
            return TurnResult(text, i, status, root.trace_id, completed)

        self._ckpt.mark(session_key, turn_id, "done", ts=self._clock())
        self._tracer.end_span(root, end_ts=self._clock(), status=status)
        return TurnResult(text, i + 1, status, root.trace_id, completed)

    async def _execute_tool_call(
        self,
        tc: ToolCall,
        ctx: Context,
        completed: list[str],
        completed_sigs: dict[str, object],
        *,
        session_ctx,
        root,
        session_key: str,
        turn_id: str,
        iteration: int,
        consult_cache: bool,
        allowed_tools: set[str] | None = None,
    ) -> None:
        """Chạy 1 tool_call (hoặc trả lại kết quả cache nếu `consult_cache` và đã có chữ ký
        khớp — RT-2), ghi tool_result vào context, rồi checkpoint NGAY (không đợi hết cả
        batch) để resume/hủy giữa batch vẫn không mất tiến độ (RT-3).

        `consult_cache` CHỈ bật khi turn này đang resume: trong một lần chạy tươi (không
        resume), tool-call lặp lại phải chạy THẬT để cơ chế anti-loop (force_text_after_repeats)
        còn phát hiện được — nếu luôn tra cache thì mọi lần lặp thứ 2 trở đi sẽ bị nuốt âm
        thầm, anti-loop không bao giờ kích hoạt.

        `allowed_tools` (RT-8): truyền THẲNG xuống `execute_tool` để enforce toolset con lúc
        THỰC THI — trước đây chỉ ẩn schema khỏi provider (`self._registry.schemas(allowed_tools)`
        ở `run_turn`), không chặn ở lớp chạy tool nên model vẫn có thể gọi tool ngoài toolset.
        """
        sig = tool_call_signature(tc.name, tc.args)
        if consult_cache and sig in completed_sigs:
            cached = completed_sigs[sig]
            if isinstance(cached, dict):
                content = str(cached.get("content", ""))
                is_error = bool(cached.get("is_error", False))
            else:
                content = str(cached)
                is_error = False
            ctx.add_tool_result(tc.id, content, is_error=is_error)
            completed.append(tc.id)
        else:
            tspan = self._tracer.start_span(
                SpanKind.TOOL_CALL, tc.name, start_ts=self._clock(),
                trace_id=root.trace_id, parent_id=root.id, session_key=session_key,
            )
            result = await execute_tool(
                tc.name, tc.args, session_ctx,
                gate=self._gate, registry=self._registry,
                approver=self._approver, auditor=self._auditor, hooks=self._hooks,
                allowed_tools=allowed_tools,
            )
            # span_attrs từ tool (vd cost_usd cho web_search) — KHÔNG ghi đè is_error.
            ledger = {
                k: v for k, v in result.span_attrs.items() if k != "is_error"
            }
            self._tracer.end_span(
                tspan, end_ts=self._clock(), is_error=result.is_error, **ledger
            )
            ctx.add_tool_result(tc.id, result.content, is_error=result.is_error)
            completed.append(tc.id)
            completed_sigs[sig] = {"content": result.content, "is_error": result.is_error}
        self._ckpt.save(
            session_key, turn_id, iteration,
            {"history": serialize_messages(ctx.messages), "completed_sigs": completed_sigs},
            ts=self._clock(), status="running",
        )

    def _emit_event(self, root, name: str, session_key: str) -> None:
        ev = self._tracer.start_span(
            SpanKind.EVENT, name, start_ts=self._clock(),
            trace_id=root.trace_id, parent_id=root.id, session_key=session_key,
        )
        self._tracer.end_span(ev, end_ts=self._clock())

    def _router_name(self) -> str:
        return "provider"


class _SimpleCtx:
    def __init__(self, session_key: str) -> None:
        self.session_key = session_key
