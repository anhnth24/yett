"""Agent loop (spec P0-P1 §4, WP1.2).

ContextStage → [Think → Prune → Tool → Observe → Checkpoint]×N → Finalize.
Bất biến: bounded iterations; mỗi tool call qua wiring (Gate→...→Filters); checkpoint
để resume không lặp side-effect; cancel sạch ở ranh giới stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from yett.core.cancel import Cancelled, CancelToken
from yett.core.context import Context
from yett.obs.tracer import SpanKind
from yett.provider.base import ChatResult
from yett.provider.failover import ContextOverflow, FailoverRouter
from yett.security.gate import PolicyGate
from yett.tools.registry import Registry
from yett.tools.wiring import Approver, Auditor, execute_tool

# clock injectable để test deterministic (spec §0).
Clock = Callable[[], float]


@dataclass
class LoopConfig:
    max_iterations: int = 20
    context_token_budget: int = 150_000


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

    async def run_turn(
        self,
        ctx: Context,
        *,
        session_key: str,
        turn_id: str,
        cancel: CancelToken | None = None,
        session_ctx=None,
    ) -> TurnResult:
        cancel = cancel or CancelToken()
        session_ctx = session_ctx or _SimpleCtx(session_key)
        root = self._tracer.start_span(
            SpanKind.AGENT, "turn", start_ts=self._clock(), session_key=session_key
        )
        completed: list[str] = []
        # resume: bỏ qua tool_call đã hoàn thành ở lần chạy trước
        prev = self._ckpt.load(session_key, turn_id)
        if prev and prev["status"] == "running":
            completed = list(prev["state"].get("completed_tool_calls", []))

        status = "done"
        text = ""
        i = 0
        try:
            for i in range(self._cfg.max_iterations):
                cancel.check()
                # THINK
                span = self._tracer.start_span(
                    SpanKind.LLM_CALL, "think", start_ts=self._clock(),
                    trace_id=root.trace_id, parent_id=root.id, session_key=session_key,
                )
                try:
                    res = await self._router.chat(ctx.messages, self._registry.schemas())
                except ContextOverflow:
                    ctx.prune(self._cfg.context_token_budget // 2)
                    self._tracer.end_span(span, end_ts=self._clock(), event="context_overflow_pruned")
                    continue
                self._tracer.end_span(
                    span, end_ts=self._clock(),
                    provider=self._router_name(), model=res.raw_model,
                    input_tokens=res.usage.input_tokens, output_tokens=res.usage.output_tokens,
                    cost_usd=self._cost_fn(res),
                )
                if res.stop_reason == "end_turn":
                    text = res.text or ""
                    ctx.add_assistant(text)
                    break
                # PRUNE
                if ctx.tokens() > self._cfg.context_token_budget:
                    ctx.prune(self._cfg.context_token_budget)
                # TOOL + OBSERVE
                for tc in res.tool_calls:
                    cancel.check()
                    if tc.id in completed:
                        continue  # idempotency khi resume
                    tspan = self._tracer.start_span(
                        SpanKind.TOOL_CALL, tc.name, start_ts=self._clock(),
                        trace_id=root.trace_id, parent_id=root.id, session_key=session_key,
                    )
                    result = await execute_tool(
                        tc.name, tc.args, session_ctx,
                        gate=self._gate, registry=self._registry,
                        approver=self._approver, auditor=self._auditor,
                    )
                    self._tracer.end_span(tspan, end_ts=self._clock(), is_error=result.is_error)
                    ctx.add_tool_result(tc.id, result.content)
                    completed.append(tc.id)
                # CHECKPOINT
                self._ckpt.save(
                    session_key, turn_id, i,
                    {"completed_tool_calls": completed}, ts=self._clock(), status="running",
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
