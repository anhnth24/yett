"""Contract test cho OpenAICompatProvider chạy qua AgentLoop thật (không FakeProvider).

FakeProvider bỏ qua toàn bộ cấu trúc `messages` gửi lên API — nên 2 lỗi P0 sau vô hình
với mọi test khác trong repo:
  - P0-3: `Context.system` (system prompt) không bao giờ được đưa vào `ctx.messages`,
    nên OpenAICompatProvider._to_openai_msg không có gì để gửi role="system".
  - P0-2: khi model trả tool_calls, `AgentLoop.run_turn` chỉ gọi `ctx.add_assistant()`
    ở nhánh kết thúc turn (end_turn/force_text) — turn có tool_use KHÔNG được thêm vào
    context. Message "tool" bị nối thẳng sau message "user" trước đó, sai thứ tự chuẩn
    OpenAI (mọi message role=tool phải theo ngay sau message role=assistant có tool_calls
    khớp id).

http_post được inject (offline, không gọi mạng thật) — recorder ghi lại đúng body đã gửi
để assert cấu trúc.

Phase 2 đã sửa `loop.py`/`context.py` (system message + assistant tool_use turn) nên test
này giờ PASS bình thường — không còn `xfail`.
"""

from __future__ import annotations

import itertools

from yett.config.models import SecurityCfg, ToolRule
from yett.core.checkpoint import CheckpointStore
from yett.core.context import assemble_context
from yett.core.loop import AgentLoop, LoopConfig
from yett.obs.spanstore import SpanStore
from yett.provider.failover import FailoverRouter
from yett.provider.openai_compat import OpenAICompatProvider
from yett.security.basic_gate import BasicGate
from yett.security.filters import redact_attrs
from yett.tools.base import ToolResult
from yett.tools.registry import Registry


def _clock():
    counter = itertools.count(1)
    return lambda: float(next(counter))


class _EchoTool:
    """Tool tối giản chỉ để kích hoạt một vòng tool_use trong loop."""

    name = "echo"
    schema = {"type": "object", "properties": {}}

    def validate(self, args: dict) -> None:
        return None

    async def run(self, args: dict, ctx) -> ToolResult:
        return ToolResult.success("ok")


def _tool_call_response(call_id: str, name: str, args_json: str) -> dict:
    return {
        "model": "test-model",
        "choices": [{
            "message": {"content": None, "tool_calls": [
                {"id": call_id, "function": {"name": name, "arguments": args_json}},
            ]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_response(text: str) -> dict:
    return {
        "model": "test-model",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def test_real_provider_sends_system_prompt_and_valid_tool_ordering(tmp_path) -> None:
    recorded: list[dict] = []

    async def recorder(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        recorded.append(body)
        if len(recorded) == 1:
            return 200, _tool_call_response("call-1", "echo", "{}")
        return 200, _text_response("xong rồi")

    provider = OpenAICompatProvider(
        model="test-model", api_key="test-key", base_url="https://example.invalid/v1",
        http_post=recorder,
    )
    router = FailoverRouter(provider)
    rules = [ToolRule(tool="echo", arg_patterns={}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    reg = Registry()
    reg.register(_EchoTool())
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    ckpt = CheckpointStore(tmp_path / "ckpt.db")
    loop = AgentLoop(router, gate, reg, store, ckpt, cfg=LoopConfig(max_iterations=5), clock=_clock())

    ctx = assemble_context("Bạn là yett.", "chạy echo giúp em")
    res = await loop.run_turn(ctx, session_key="s1", turn_id="t1")

    assert res.status == "done"
    assert len(recorded) == 2  # 1 lần THINK trước tool + 1 lần THINK sau tool result

    # (a) provider phải nhận system prompt trong MỌI request, kể cả request đầu tiên.
    first_body = recorded[0]
    assert first_body["messages"], "request rỗng — không gửi message nào"
    assert first_body["messages"][0]["role"] == "system"
    assert first_body["messages"][0]["content"] == "Bạn là yett."

    # (b) thứ tự OpenAI hợp lệ: mọi message role="tool" phải đứng NGAY SAU một message
    # role="assistant" mang tool_calls chứa đúng id đó.
    second_body = recorded[1]
    messages = second_body["messages"]
    for i, m in enumerate(messages):
        if m["role"] != "tool":
            continue
        assert i > 0, "message 'tool' không thể là message đầu tiên"
        prev = messages[i - 1]
        assert prev["role"] == "assistant", (
            f"message 'tool' (tool_call_id={m.get('tool_call_id')!r}) phải đứng ngay sau "
            f"message 'assistant' mang tool_calls, nhưng đứng sau role={prev['role']!r}"
        )
        prev_ids = {tc["id"] for tc in prev.get("tool_calls") or []}
        assert m.get("tool_call_id") in prev_ids, (
            f"tool_call_id={m.get('tool_call_id')!r} không khớp id nào trong "
            f"assistant.tool_calls={prev_ids!r}"
        )

    store.close()
    ckpt.close()
