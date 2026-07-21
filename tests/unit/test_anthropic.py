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
    assert "temperature" not in captured["body"]
    assert "Authorization" not in captured["headers"]


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


async def test_failed_tool_result_sets_anthropic_is_error() -> None:
    captured: dict = {}

    async def transport(url, headers, body):
        captured["body"] = body
        return 200, _ok(text="handled")

    await _provider(transport).chat(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="toolu_1", name="exec", args={"cmd": "false"})],
            ),
            Message(
                role="tool",
                content="exit 1",
                tool_call_id="toolu_1",
                tool_result_is_error=True,
            ),
        ],
        [],
    )
    assert captured["body"]["messages"][1]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "exit 1",
            "is_error": True,
        }
    ]


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
    assert res.provider_name == "anthropic"
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


@pytest.mark.parametrize(
    ("api_reason", "canonical_reason"),
    [
        ("end_turn", "end_turn"),
        ("stop_sequence", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
    ],
)
async def test_stop_reasons_are_mapped_exactly(
    api_reason: str, canonical_reason: str
) -> None:
    async def transport(url, headers, body):
        return 200, _ok(text="x", stop=api_reason)

    res = await _provider(transport).chat([Message(role="user", content="hi")], [])
    assert res.stop_reason == canonical_reason


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
        (402, FailReason.AUTH),
        (403, FailReason.AUTH),
        (404, FailReason.BAD_REQUEST),
        (429, FailReason.RATE_LIMIT),
        (529, FailReason.OVERLOADED),
        (408, FailReason.TIMEOUT),
        (504, FailReason.TIMEOUT),
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


async def test_invalid_output_max_tokens_is_not_context_overflow() -> None:
    async def transport(url, headers, body):
        return 400, {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "max_tokens 200000 exceeds the maximum output token limit of 128000",
            },
        }

    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.BAD_REQUEST


async def test_timeout_from_transport_propagates_for_retry() -> None:
    async def transport(url, headers, body):
        raise ProviderError(FailReason.TIMEOUT, "request timed out")

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.TIMEOUT


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("slow"), FailReason.TIMEOUT),
        (OSError("connection reset"), FailReason.NETWORK),
    ],
)
async def test_native_transport_exceptions_are_canonical(error: Exception, reason: FailReason) -> None:
    async def transport(url, headers, body):
        raise error

    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == reason


async def test_default_transport_enforces_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    def fake_urlopen(request, *, timeout):
        assert timeout == 0.25
        raise TimeoutError("slow")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    p = AnthropicProvider(model="claude-haiku-4-5", api_key=SECRET, timeout_sec=0.25)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.TIMEOUT


async def test_default_transport_classifies_malformed_json_as_server_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response())
    p = AnthropicProvider(model="claude-haiku-4-5", api_key=SECRET)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.SERVER_5XX


async def test_malformed_response_is_retryable_server_failure() -> None:
    async def transport(url, headers, body):
        return 200, {"model": "x", "stop_reason": "end_turn"}  # thiếu content list

    p = _provider(transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.SERVER_5XX


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "message",
            "role": "assistant",
            "model": "x",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "exec", "input": "{"}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "message",
            "role": "assistant",
            "model": "x",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "exec", "input": {}}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "message",
            "role": "assistant",
            "model": "x",
            "content": [],
            "stop_reason": "pause_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ],
)
async def test_protocol_invalid_success_is_retryable_server_failure(payload: dict) -> None:
    async def transport(url, headers, body):
        return 200, payload

    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.SERVER_5XX


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


async def test_secret_crossing_error_preview_boundary_is_fully_redacted() -> None:
    async def transport(url, headers, body):
        return 401, {"error": {"message": "x" * 185 + SECRET}}

    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat([Message(role="user", content="hi")], [])
    assert SECRET not in str(ei.value)
    assert SECRET[:8] not in str(ei.value)


async def test_tool_result_without_id_is_rejected_before_network() -> None:
    called = False

    async def transport(url, headers, body):
        nonlocal called
        called = True
        return 200, _ok(text="unexpected")

    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat([Message(role="tool", content="oops")], [])
    assert ei.value.reason == FailReason.BAD_REQUEST
    assert not called


async def test_unmatched_or_missing_tool_results_are_rejected_before_network() -> None:
    async def transport(url, headers, body):
        return 200, _ok(text="unexpected")

    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="toolu_1", name="exec", args={})],
    )
    with pytest.raises(ProviderError, match="no matching"):
        await _provider(transport).chat(
            [assistant, Message(role="tool", content="x", tool_call_id="wrong")],
            [],
        )
    with pytest.raises(ProviderError, match="missing tool results"):
        await _provider(transport).chat([assistant], [])


async def test_non_json_tool_args_are_rejected_before_network() -> None:
    called = False

    async def transport(url, headers, body):
        nonlocal called
        called = True
        return 200, _ok(text="unexpected")

    message = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="toolu_1", name="exec", args={"value": float("nan")})],
    )
    with pytest.raises(ProviderError) as ei:
        await _provider(transport).chat(
            [message, Message(role="tool", content="x", tool_call_id="toolu_1")],
            [],
        )
    assert ei.value.reason == FailReason.BAD_REQUEST
    assert not called


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
