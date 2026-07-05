"""Test resume/checkpoint (RT-2, RT-3, RT-12 — xem plans/260705-1658-harden-yett-harness).

RT-2: checkpoint phải lưu ĐẦY ĐỦ lịch sử message (assistant tool_use + nội dung tool_result),
không chỉ id — id do provider cấp không sống sót qua resume (provider cấp id mới mỗi lần
gọi). Idempotency khi resume dựa trên chữ ký ổn định `tool_name + hash(args)`.
RT-3: checkpoint ở trạng thái "canceled" (hủy sạch giữa turn) phải resumable như "running"
— không chỉ "running" mới khôi phục được, nếu không resume sẽ chạy lại tool đã hoàn thành.
RT-12: ước lượng token phải tính cả args của tool_call (không chỉ content) và prune() phải
rút gọn cả cặp tool_use/tool_result cùng nhau — nếu không, phần token nặng nằm ở args sẽ
không bao giờ giảm, khiến ContextOverflow lặp vô ích tới hết max_iterations (livelock).
"""

from __future__ import annotations

import itertools
import json

from yett.config.models import SecurityCfg, ToolRule
from yett.core.cancel import CancelToken
from yett.core.checkpoint import CheckpointStore
from yett.core.context import assemble_context, tool_call_signature
from yett.core.loop import AgentLoop, LoopConfig
from yett.obs.spanstore import SpanStore
from yett.provider.base import ChatResult, FailReason, Message, ToolCall, Usage
from yett.provider.failover import FailoverRouter, ProviderError
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.security.basic_gate import BasicGate
from yett.security.filters import redact_attrs
from yett.tools.base import ToolResult
from yett.tools.registry import Registry


def _clock():
    counter = itertools.count(1)
    return lambda: float(next(counter))


class _EchoTool:
    name = "echo"
    schema = {"type": "object", "properties": {}}

    def validate(self, args: dict) -> None:
        return None

    async def run(self, args: dict, ctx) -> ToolResult:
        return ToolResult.success("echoed-value")


def _gate(tool_name: str) -> BasicGate:
    return BasicGate(SecurityCfg(allowlist=[ToolRule(tool=tool_name, arg_patterns={}, effect="allow")]))


async def test_checkpoint_persists_full_message_history_not_just_ids(tmp_path) -> None:
    """RT-2: state phải chứa assistant tool_use (tool_calls) + nội dung tool_result thật,
    không chỉ id — để resume rebuild Context mà không cần hỏi lại model từ đầu."""
    reg = Registry()
    reg.register(_EchoTool())
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CheckpointStore(tmp_path / "ckpt.db")
    provider = FakeProvider([tool_result("c1", "echo", {"x": 1}), text_result("xong")])
    loop = AgentLoop(
        FailoverRouter(provider), _gate("echo"), reg, store, ckpt,
        cfg=LoopConfig(max_iterations=5), clock=_clock(),
    )
    ctx = assemble_context("sys", "go")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")
    assert res.status == "done"

    saved = ckpt.load("s1", "t1")
    history = saved["state"]["history"]
    assistant_tool_use = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant_tool_use["tool_calls"][0]["name"] == "echo"
    assert assistant_tool_use["tool_calls"][0]["args"] == {"x": 1}
    tool_msg = next(m for m in history if m["role"] == "tool")
    assert tool_msg["content"] == "echoed-value"  # nội dung THẬT của tool_result, không chỉ id
    assert tool_msg["tool_call_id"] == "c1"
    store.close()
    ckpt.close()


async def test_resume_after_cancel_does_not_rerun_tool(tmp_path) -> None:
    """RT-3: checkpoint 'canceled' phải resumable như 'running'. Trước fix, guard resume chỉ
    nhận 'running' → resume sau cancel nạp completed=[] và chạy lại tool đã có side-effect."""
    cancel = CancelToken()

    class _CancelingTool:
        name = "sideeffect"
        schema = {"type": "object", "properties": {}}

        def __init__(self) -> None:
            self.runs = 0

        def validate(self, args: dict) -> None:
            return None

        async def run(self, args: dict, ctx) -> ToolResult:
            self.runs += 1
            cancel.cancel()  # mô phỏng tín hiệu hủy đến ngay sau khi tool chạy xong
            return ToolResult.success(f"ran #{self.runs}")

    tool = _CancelingTool()
    reg = Registry()
    reg.register(tool)
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CheckpointStore(tmp_path / "ckpt.db")
    provider = FakeProvider([tool_result("c1", "sideeffect", {})])
    loop = AgentLoop(
        FailoverRouter(provider), _gate("sideeffect"), reg, store, ckpt,
        cfg=LoopConfig(max_iterations=5), clock=_clock(),
    )
    ctx = assemble_context("sys", "làm")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1", cancel=cancel)

    assert res.status == "canceled"
    assert tool.runs == 1
    saved = ckpt.load("s1", "t1")
    assert saved["status"] == "canceled"
    sig = tool_call_signature("sideeffect", {})
    assert sig in saved["state"]["completed_sigs"]
    store.close()

    # Resume cùng turn_id sau CANCEL (không phải crash) — model (kịch bản mới) lại đòi chạy
    # đúng tool/args cũ; tool KHÔNG được chạy lại thật (idempotent theo chữ ký, RT-2).
    tool2 = _CancelingTool()
    reg2 = Registry()
    reg2.register(tool2)
    store2 = SpanStore(tmp_path / "traces2.db", redactor=redact_attrs)
    ckpt2 = CheckpointStore(tmp_path / "ckpt.db")
    provider2 = FakeProvider([tool_result("c1", "sideeffect", {}), text_result("xong")])
    loop2 = AgentLoop(
        FailoverRouter(provider2), _gate("sideeffect"), reg2, store2, ckpt2,
        cfg=LoopConfig(max_iterations=5), clock=_clock(),
    )
    ctx2 = assemble_context("sys", "làm")
    res2 = await loop2.run_turn(ctx2, session_key="s1", turn_id="t1")

    assert res2.status == "done"
    assert tool2.runs == 0  # RT-3/RG1-4: resume sau cancel không lặp side-effect
    store2.close()
    ckpt2.close()


class _OverflowProvider:
    """Giả lập provider từ chối request quá lớn (ProviderError CONTEXT_OVERFLOW) cho tới khi
    context đủ nhỏ — dùng để chứng minh prune() hội tụ khi phần lớn token nằm ở tool_call
    args (RT-12), không phải livelock tới hết max_iterations."""

    def __init__(self, threshold_chars: int) -> None:
        self._threshold = threshold_chars
        self.calls = 0

    def name(self) -> str:
        return "overflow"

    def default_model(self) -> str:
        return "m"

    async def chat(self, messages, tools, *, stream=False) -> ChatResult:
        self.calls += 1
        size = 0
        for m in messages:
            size += len(m.content)
            if m.tool_calls:
                size += sum(len(json.dumps(tc.args)) for tc in m.tool_calls)
        if size > self._threshold:
            raise ProviderError(FailReason.CONTEXT_OVERFLOW)
        return ChatResult(
            text="xong", tool_calls=[], usage=Usage(1, 1), stop_reason="end_turn", raw_model="m",
        )


async def test_context_overflow_prunes_tool_call_args_and_does_not_livelock(tmp_path) -> None:
    """RT-12: token nặng nằm ở args tool_call (không phải tool result) — prune() phải rút gọn
    cả cặp mới hội tụ; nếu chỉ nhắm tool result, ContextOverflow lặp tới hết max_iterations
    mà không giảm được token nào."""
    reg = Registry()
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CheckpointStore(tmp_path / "ckpt.db")
    provider = _OverflowProvider(threshold_chars=1000)
    loop = AgentLoop(
        FailoverRouter(provider), _gate("noop"), reg, store, ckpt,
        cfg=LoopConfig(max_iterations=5, context_token_budget=200), clock=_clock(),
    )
    ctx = assemble_context("sys", "hi")
    # Mô phỏng một turn trước đó đã để lại 1 cặp tool_use/tool_result rất nặng (args lớn).
    ctx.add_assistant_tool_calls([ToolCall("c1", "search", {"q": "a" * 3000})])
    ctx.add_tool_result("c1", "b" * 3000)

    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")

    assert res.status == "done"
    assert res.text == "xong"
    # Hội tụ ngay sau 1 lần prune — KHÔNG lặp tới hết max_iterations (livelock).
    assert provider.calls == 2
    pruned_call = next(m for m in ctx.messages if m.role == "assistant" and m.tool_calls)
    assert pruned_call.tool_calls[0].args == {"_pruned": True}
    store.close()
    ckpt.close()


def test_estimate_tokens_counts_tool_call_args() -> None:
    """RT-12: token estimate phải tính args tool_call — content assistant tool_use thường
    rỗng nhưng args có thể nặng; bỏ qua sẽ ước lượng ~0 cho một lượt tool_use lớn."""
    ctx = assemble_context("sys", "hi")
    big_args = {"query": "x" * 4000}
    ctx.add_assistant_tool_calls([ToolCall("c1", "search", big_args)])
    baseline = ctx.tokens()
    ctx.messages[-1] = Message(role="assistant", content="")  # cùng message nhưng thiếu tool_calls
    without_args = ctx.tokens()
    assert baseline - without_args >= len(json.dumps(big_args)) // 4 - 1
