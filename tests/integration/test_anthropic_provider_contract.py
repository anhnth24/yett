"""Contract: AnthropicProvider qua AgentLoop thật (transport inject, không gọi mạng).

Song song với test_real_provider_contract (OpenAI-compat): xác nhận system prompt →
top-level `system`, và assistant tool_use → user tool_result đúng thứ tự Messages API.
"""

from __future__ import annotations

import itertools

from yett.config.models import SecurityCfg, ToolRule
from yett.core.checkpoint import CheckpointStore
from yett.core.context import assemble_context
from yett.core.loop import AgentLoop, LoopConfig
from yett.obs.spanstore import SpanStore
from yett.provider.anthropic import AnthropicProvider
from yett.provider.failover import FailoverRouter
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
        return ToolResult.error("simulated tool failure")


def _tool_use_response(call_id: str, name: str) -> dict:
    return {
        "id": "msg_tool",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
    }


def _text_response(text: str) -> dict:
    return {
        "id": "msg_text",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
    }


async def test_anthropic_loop_system_prompt_and_tool_result_ordering(tmp_path) -> None:
    recorded: list[dict] = []

    async def recorder(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        recorded.append(body)
        if len(recorded) == 1:
            return 200, _tool_use_response("toolu_1", "echo")
        return 200, _text_response("xong rồi")

    provider = AnthropicProvider(
        model="claude-haiku-4-5",
        api_key="test-key-not-for-network",
        base_url="https://example.invalid/v1",
        http_post=recorder,
    )
    router = FailoverRouter(provider)
    rules = [ToolRule(tool="echo", arg_patterns={}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    reg = Registry()
    reg.register(_EchoTool())
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CheckpointStore(tmp_path / "ckpt.db")
    loop = AgentLoop(
        router, gate, reg, store, ckpt, cfg=LoopConfig(max_iterations=5), clock=_clock()
    )

    ctx = assemble_context("Bạn là yett.", "chạy echo giúp em")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")

    assert res.status == "done"
    assert len(recorded) == 2

    first = recorded[0]
    assert first["system"] == "Bạn là yett."
    assert all(m["role"] != "system" for m in first["messages"])
    assert first["messages"][0]["role"] == "user"

    second = recorded[1]
    messages = second["messages"]
    # Tìm user message mang tool_result — phải đứng ngay sau assistant mang tool_use khớp id.
    for i, m in enumerate(messages):
        if m["role"] != "user" or not isinstance(m.get("content"), list):
            continue
        results = [b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        if not results:
            continue
        assert i > 0
        prev = messages[i - 1]
        assert prev["role"] == "assistant"
        prev_ids = {
            b["id"] for b in prev.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"
        }
        for tr in results:
            assert tr["tool_use_id"] in prev_ids
            assert tr["is_error"] is True

    store.close()
    ckpt.close()
