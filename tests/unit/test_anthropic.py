"""Contract tests offline cho Anthropic Messages API native adapter.

Transport inject — không gọi mạng. Bao phủ request shape, system prompt, tools,
assistant tool_use → user tool_result, parse response/usage, lỗi/retry classification,
timeout, malformed, secret không lộ.
"""

from __future__ import annotations

import pytest

from yett.config.models import ProviderCfg
from yett.obs.cost import compute_cost
from yett.provider.anthropic import AnthropicProvider
from yett.provider.base import FailReason, Message, ToolCall, ToolSchema
from yett.provider.factory import build_provider
from yett.provider.failover import ProviderError
from yett.secrets.backends import InMemorySecretStore

SECRET = "sk-ant-secret-KEY-DO-NOT-LEAK-1234567890abcdef"


def _ok(
    *,
    text: str | None = None,
    tool_uses: list[dict] | None = None,
    usage: dict | None = None,
    stop: str = "end_turn",
    model: str = "claude-haiku-4-5",
) -> dict:
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append(tu)
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "usage": usage
        or {
            "input_tokens": 12,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
        },
    }


def _provider(transport, *, key: str = SECRET) -> AnthropicProvider:
    return AnthropicProvider(
        model="claude-haiku-4-5",
        api_key=key,
        base_url="https://api.anthropic.com/v1",
        http_post=transport,
    )


async def test_request_shape_headers_and_endpoint() -> None:
    captured: dict = {}

    async def transport(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, _ok(text="hi")

    p = _provider(transport)
    await p.chat([Message(role="user", content="ping")], [])
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == SECRET
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["model"] == "claude-haiku-4-5"
    assert captured["body"]["max_tokens"] == 4096
    assert "Authorization" not in captured["headers"]


async def test_streaming_is_rejected_instead_of_silently_ignored() -> None:
    called = False

    async def transport(url, headers, body):
        nonlocal called
        called = True
        return 200, _ok(text="unexpected")

    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat([Message(role="user", content="ping")], [], stream=True)
    assert ei.value.reason == FailReason.BAD_REQUEST
    assert not called


async def test_adaptive_thinking_model_disables_thinking_and_omits_temperature() -> None:
    captured: dict = {}

    async def transport(url, headers, body):
        captured["body"] = body
        return 200, _ok(text="ok", model="claude-sonnet-5")

    p = AnthropicProvider(
        model="claude-sonnet-5",
        api_key=SECRET,
        http_post=transport,
    )
    await p.chat([Message(role="user", content="ping")], [])
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert "temperature" not in captured["body"]


async def test_system_prompt_is_top_level_not_in_messages() -> None:
    captured: dict = {}

    async def transport(url, headers, body):
        captured["body"] = body
        return 200, _ok(text="ok")

    p = _provider(transport)
    msgs = [
        Message(role="system", content="Bạn là yett."),
        Message(role="user", content="chào"),
    ]
    await p.chat(msgs, [])
    body = captured["body"]
    assert body["system"] == "Bạn là yett."
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"][0] == {"role": "user", "content": "chào"}


async def test_multiple_system_messages_concatenated() -> None:
    captured: dict = {}

    async def transport(url, headers, body):
        captured["body"] = body
        return 200, _ok(text="ok")

    p = _provider(transport)
    await p.chat(
        [
            Message(role="system", content="A"),
            Message(role="system", content="B"),
            Message(role="user", content="hi"),
        ],
        [],
    )
    assert captured["body"]["system"] == "A\n\nB"


async def test_tool_definitions_use_input_schema() -> None:
    captured: dict = {}

    async def transport(url, headers, body):
        captured["body"] = body
        return 200, _ok(text="ok")

    tools = [ToolSchema("exec", "run a command", {"type": "object", "properties": {"cmd": {"type": "string"}}})]
    p = _provider(transport)
    await p.chat([Message(role="user", content="run")], tools)
    assert captured["body"]["tools"] == [
        {
            "name": "exec",
            "description": "run a command",
            "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }
    ]


async def test_assistant_tool_use_then_user_tool_result_conversion() -> None:
    """Internal Message(role=tool) → Anthropic user message với tool_result blocks (gộp)."""
    captured: dict = {}

    async def transport(url, headers, body):
        captured["body"] = body
        return 200, _ok(text="xong")

    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="chạy ls"),
        Message(
            role="assistant",
            content="để em gọi tool",
            tool_calls=[
                ToolCall(id="toolu_1", name="exec", args={"cmd": "ls"}),
                ToolCall(id="toolu_2", name="exec", args={"cmd": "pwd"}),
            ],
        ),
        Message(role="tool", content="a.txt", tool_call_id="toolu_1"),
        Message(role="tool", content="/tmp", tool_call_id="toolu_2"),
    ]
    p = _provider(transport)
    await p.chat(msgs, [ToolSchema("exec", "run", {"type": "object"})])
    api_msgs = captured["body"]["messages"]
    assert api_msgs[0]["role"] == "user" and api_msgs[0]["content"] == "chạy ls"
    assert api_msgs[1]["role"] == "assistant"
    assert api_msgs[1]["content"][0] == {"type": "text", "text": "để em gọi tool"}
    assert api_msgs[1]["content"][1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "exec",
        "input": {"cmd": "ls"},
    }
    assert api_msgs[1]["content"][2]["id"] == "toolu_2"
    # Consecutive tool results → một user message
    assert api_msgs[2]["role"] == "user"
    assert api_msgs[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "/tmp"},
    ]
    assert captured["body"]["system"] == "sys"


async def test_text_response_and_usage_parsed() -> None:
    async def transport(url, headers, body):
        return 200, _ok(
            text="Chào anh",
            usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 40},
        )

    p = _provider(transport)
    res = await p.chat([Message(role="user", content="hi")], [])
    assert res.text == "Chào anh"
    assert res.stop_reason == "end_turn"
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 20
    assert res.usage.cache_read_tokens == 40
    assert res.raw_model == "claude-haiku-4-5"
    assert res.tool_calls == []


async def test_tool_use_response_parsed() -> None:
    async def transport(url, headers, body):
        return 200, _ok(
            tool_uses=[
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "exec",
                    "input": {"cmd": "ls"},
                }
            ],
            stop="tool_use",
        )

    p = _provider(transport)
    res = await p.chat(
        [Message(role="user", content="chạy")],
        [ToolSchema("exec", "run", {})],
    )
    assert res.stop_reason == "tool_use"
    assert res.tool_calls[0].id == "toolu_abc"
    assert res.tool_calls[0].name == "exec"
    assert res.tool_calls[0].args == {"cmd": "ls"}


async def test_usage_feeds_cost_ledger() -> None:
    """Usage + pricing anthropic → compute_cost > 0 (tracing path App dùng)."""
    from yett.config.models import PriceRow

    async def transport(url, headers, body):
        return 200, _ok(
            text="ok",
            usage={
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "cache_read_input_tokens": 1_000_000,
            },
        )

    p = _provider(transport)
    res = await p.chat([Message(role="user", content="hi")], [])
    pricing = {
        "anthropic": {
            "claude-haiku-4-5": PriceRow(
                input_per_mtok=1.0, output_per_mtok=5.0, cache_read_per_mtok=0.1
            ),
        }
    }
    cost = compute_cost(p.name(), res.raw_model, res.usage, pricing)
    assert cost == pytest.approx(6.1)


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, FailReason.AUTH),
        (403, FailReason.AUTH),
        (429, FailReason.RATE_LIMIT),
        (529, FailReason.OVERLOADED),
        (408, FailReason.TIMEOUT),
        (500, FailReason.SERVER_5XX),
        (502, FailReason.SERVER_5XX),
        (400, FailReason.BAD_REQUEST),
    ],
)
async def test_http_errors_map_to_fail_reasons(status: int, reason: FailReason) -> None:
    async def transport(url, headers, body):
        return status, {"error": {"type": "api_error", "message": "boom"}}

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == reason


async def test_context_overflow_classification() -> None:
    async def transport(url, headers, body):
        return 400, {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "prompt is too long: 200000 tokens > 200000 maximum",
            },
        }

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.CONTEXT_OVERFLOW


async def test_timeout_from_transport_propagates_for_retry() -> None:
    async def transport(url, headers, body):
        raise ProviderError(FailReason.TIMEOUT, "request timed out")

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.TIMEOUT


async def test_malformed_response_raises_bad_request() -> None:
    async def transport(url, headers, body):
        return 200, {"model": "x", "stop_reason": "end_turn"}  # thiếu content list

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.BAD_REQUEST


async def test_secret_not_disclosed_in_error_message() -> None:
    async def transport(url, headers, body):
        # Server echo key (mô phỏng) — adapter phải redact trước khi raise.
        return 401, {"error": {"message": f"invalid key {SECRET}"}}

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert SECRET not in str(ei.value)
    assert SECRET not in repr(ei.value)
    assert "[REDACTED]" in str(ei.value)


async def test_secret_not_in_malformed_payload_text() -> None:
    async def transport(url, headers, body):
        return 200, {"content": "not-a-list", "note": SECRET}

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert SECRET not in str(ei.value)


def test_factory_builds_anthropic_from_secret_store() -> None:
    secrets = InMemorySecretStore({"llm_key": SECRET})
    cfg = ProviderCfg(name="anthropic", model="claude-haiku-4-5", api_key_secret="llm_key")
    p = build_provider(cfg, secrets)
    assert p.name() == "anthropic"
    assert p.default_model() == "claude-haiku-4-5"
    assert isinstance(p, AnthropicProvider)


def test_factory_anthropic_uses_default_base_url() -> None:
    secrets = InMemorySecretStore({"llm_key": "k"})
    cfg = ProviderCfg(name="anthropic", model="claude-sonnet-5", api_key_secret="llm_key")
    p = build_provider(cfg, secrets)
    assert p._base == "https://api.anthropic.com/v1"  # type: ignore[attr-defined]


def test_factory_anthropic_inline_key() -> None:
    secrets = InMemorySecretStore({})
    cfg = ProviderCfg(name="anthropic", model="claude-haiku-4-5", api_key=SECRET)
    p = build_provider(cfg, secrets)
    assert p._key == SECRET  # type: ignore[attr-defined]


def test_wizard_catalog_includes_anthropic_native() -> None:
    from yett.setup_wizard import Answers, build_config, find_provider

    choice = find_provider("anthropic")
    assert choice is not None
    assert "native" in choice.label.lower() or "Messages" in choice.label
    cfg = build_config(Answers(provider="anthropic", model="claude-haiku-4-5"))
    assert cfg["provider"]["name"] == "anthropic"
    assert "api.anthropic.com" in cfg["egress"]["allowlist"]
    assert "api_key" not in cfg["provider"]  # secret chỉ theo tên
    assert cfg["provider"]["api_key_secret"] == "llm_key"
