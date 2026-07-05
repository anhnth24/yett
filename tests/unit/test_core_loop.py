"""Test agent loop: end-to-end turn, tool use, resume idempotency, cancel, max-iter.

Map gate: RG1-4 (resume không lặp side-effect), RG1-6 (deterministic), RG1-10 (cancel sạch),
RG1-5 (span có cost).
"""

from __future__ import annotations

import itertools

import pytest

from yett.config.models import SecurityCfg, ToolRule
from yett.core.cancel import CancelToken
from yett.core.checkpoint import CheckpointStore
from yett.core.context import assemble_context
from yett.core.loop import AgentLoop, LoopConfig
from yett.obs.spanstore import SpanStore
from yett.provider.failover import FailoverRouter
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.security.basic_gate import BasicGate
from yett.security.filters import redact_attrs
from yett.tools.base import Tool, ToolResult
from yett.tools.registry import Registry


def _clock():
    counter = itertools.count(1)
    return lambda: float(next(counter))


class _CountingTool:
    """Tool đếm số lần chạy — để chứng minh resume không chạy lại (RG1-4)."""

    name = "sideeffect"
    schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.runs = 0

    def validate(self, args: dict) -> None:
        return None

    async def run(self, args: dict, ctx) -> ToolResult:
        self.runs += 1
        return ToolResult.success(f"ran #{self.runs}")


def _build(script, tool: Tool | None = None, tmp_path=None, allow_tool="sideeffect"):
    provider = FakeProvider(script)
    router = FailoverRouter(provider)
    rules = [ToolRule(tool=allow_tool, arg_patterns={}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    reg = Registry()
    if tool:
        reg.register(tool)
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CheckpointStore(tmp_path / "ckpt.db")
    loop = AgentLoop(
        router, gate, reg, store, ckpt,
        cfg=LoopConfig(max_iterations=5), clock=_clock(),
        cost_fn=lambda r: 0.01,
    )
    return loop, store, ckpt, provider


async def test_simple_turn(tmp_path) -> None:
    loop, store, ckpt, _ = _build([text_result("xin chào")], tmp_path=tmp_path)
    ctx = assemble_context("sys", "hi")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")
    assert res.status == "done"
    assert res.text == "xin chào"
    # span có cost (RG1-5)
    spans = store.get_trace(res.trace_id)
    llm = [s for s in spans if s["kind"] == "LLM_CALL"][0]
    assert llm["attrs"]["cost_usd"] == 0.01
    store.close(); ckpt.close()


async def test_tool_use_turn(tmp_path) -> None:
    tool = _CountingTool()
    script = [tool_result("c1", "sideeffect", {}), text_result("xong")]
    loop, store, ckpt, _ = _build(script, tool=tool, tmp_path=tmp_path)
    ctx = assemble_context("sys", "làm đi")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")
    assert res.status == "done" and res.text == "xong"
    assert tool.runs == 1
    assert "c1" in res.completed_tool_calls
    store.close(); ckpt.close()


class _CrashProvider:
    """Trả tool_call ở call 1, rồi 'crash' (raise) ở call 2 — giả lập process chết giữa turn."""

    def __init__(self) -> None:
        self.calls = 0

    def name(self) -> str:
        return "crash"

    def default_model(self) -> str:
        return "m"

    async def chat(self, messages, tools, *, stream=False):
        self.calls += 1
        if self.calls == 1:
            return tool_result("c1", "sideeffect", {})
        raise RuntimeError("process bị kill giữa turn")


async def test_resume_does_not_rerun_tool(tmp_path) -> None:
    from yett.core.checkpoint import CheckpointStore as CS

    # Lần 1: chạy tool c1, checkpoint status=running, rồi crash (RuntimeError propagate).
    tool = _CountingTool()
    router = FailoverRouter(_CrashProvider())
    rules = [ToolRule(tool="sideeffect", arg_patterns={}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    reg = Registry(); reg.register(tool)
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CS(tmp_path / "ckpt.db")
    loop = AgentLoop(router, gate, reg, store, ckpt, cfg=LoopConfig(max_iterations=5), clock=_clock())
    ctx = assemble_context("sys", "làm")
    with pytest.raises(RuntimeError):
        await loop.run_turn(ctx, session_key="s1", turn_id="t1")
    assert tool.runs == 1
    # checkpoint còn "running" với completed=[c1]
    saved = ckpt.load("s1", "t1")
    assert saved["status"] == "running" and "c1" in saved["state"]["completed_tool_calls"]
    store.close()

    # Lần 2 (resume cùng turn_id): tool c1 KHÔNG chạy lại.
    tool2 = _CountingTool()
    loop2, store2, ckpt2, _ = _build(
        [tool_result("c1", "sideeffect", {}), text_result("hoàn tất")], tool=tool2, tmp_path=tmp_path
    )
    loop2._ckpt = CS(tmp_path / "ckpt.db")
    ctx2 = assemble_context("sys", "làm")
    res = await loop2.run_turn(ctx2, session_key="s1", turn_id="t1")
    assert res.status == "done"
    assert tool2.runs == 0  # RG1-4: tool đã hoàn thành không chạy lại khi resume
    store2.close(); ckpt2.close()


async def test_cancel_before_tool(tmp_path) -> None:
    tool = _CountingTool()
    loop, store, ckpt, _ = _build(
        [tool_result("c1", "sideeffect", {})], tool=tool, tmp_path=tmp_path
    )
    cancel = CancelToken()
    cancel.cancel()  # hủy ngay
    ctx = assemble_context("sys", "làm")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1", cancel=cancel)
    assert res.status == "canceled"
    assert tool.runs == 0  # RG1-10: hủy trước khi chạm tool
    store.close(); ckpt.close()


async def test_max_iterations(tmp_path) -> None:
    # provider luôn trả tool_call (args KHÁC nhau → không kích anti-loop lặp) → cạn budget.
    # Bước cuối bị ép trả lời (reserve_final_steps) nhưng status = max_iterations.
    tool = _CountingTool()
    script = [tool_result(f"c{i}", "sideeffect", {"n": i}) for i in range(10)]
    loop, store, ckpt, _ = _build(script, tool=tool, tmp_path=tmp_path)
    ctx = assemble_context("sys", "loop mãi")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")
    assert res.status == "max_iterations"
    assert res.iterations == 5
    assert tool.runs == 4  # 4 bước làm tool + 1 bước cuối ép trả lời (reserve)
    store.close(); ckpt.close()


async def test_antiloop_repeated_tool_forces_answer(tmp_path) -> None:
    # provider lặp lại y hệt một tool-call → anti-loop ép trả lời sớm (không cạn budget).
    tool = _CountingTool()
    script = [tool_result(f"c{i}", "sideeffect", {}) for i in range(10)]
    loop, store, ckpt, _ = _build(script, tool=tool, tmp_path=tmp_path)
    ctx = assemble_context("sys", "kẹt loop")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")
    assert res.status == "done"  # dừng có chủ đích, không phải cạn budget
    assert tool.runs == 3  # dừng ở ngưỡng lặp (force_text_after_repeats=3)
    spans = store.get_trace(res.trace_id)
    assert any(s["name"] == "forced_text_only" for s in spans)
    store.close(); ckpt.close()


async def test_router_caps_iterations(tmp_path) -> None:
    # max_iterations override < cfg → siết số vòng (complexity router).
    tool = _CountingTool()
    script = [tool_result(f"c{i}", "sideeffect", {"n": i}) for i in range(10)]
    loop, store, ckpt, _ = _build(script, tool=tool, tmp_path=tmp_path)
    ctx = assemble_context("sys", "câu dễ")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1", max_iterations=2)
    assert res.iterations == 2  # bị siết còn 2 vòng
    assert tool.runs == 1  # 1 tool + 1 bước ép trả lời
    store.close(); ckpt.close()


def test_context_deterministic() -> None:
    # RG1-6: cùng input → cùng context bytes
    c1 = assemble_context("sys prompt", "hello")
    c2 = assemble_context("sys prompt", "hello")
    assert [(m.role, m.content) for m in c1.messages] == [(m.role, m.content) for m in c2.messages]
    assert c1.tokens() == c2.tokens()
