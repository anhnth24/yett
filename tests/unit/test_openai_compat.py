"""Test OpenAI-compatible adapter (GLM 5.2 / MiniMax M3) offline với transport inject.

Không gọi mạng — chứng minh parse response + map lỗi + build từ factory đúng.
"""

from __future__ import annotations

import pytest

from yett.config.models import ProviderCfg
from yett.provider.base import Message, ToolSchema
from yett.provider.factory import build_provider
from yett.provider.failover import ProviderError
from yett.provider.openai_compat import OpenAICompatProvider
from yett.secrets.backends import InMemorySecretStore


def _resp(text=None, tool_calls=None, usage=None, finish="stop", model="glm-5.2"):
    msg = {"content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [{"message": msg, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 7},
    }


async def test_text_response_parsed() -> None:
    captured = {}

    async def transport(url, headers, body):
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        captured["model"] = body["model"]
        return 200, _resp(text="Chào anh")

    p = OpenAICompatProvider(model="glm-5.2", api_key="KEY", base_url="https://open.bigmodel.cn/api/paas/v4",
                             http_post=transport)
    res = await p.chat([Message(role="user", content="hi")], [])
    assert res.text == "Chào anh"
    assert res.stop_reason == "end_turn"
    assert res.usage.input_tokens == 12 and res.usage.output_tokens == 7
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer KEY"
    assert captured["model"] == "glm-5.2"


async def test_tool_call_response_parsed() -> None:
    async def transport(url, headers, body):
        return 200, _resp(
            tool_calls=[{"id": "c1", "function": {"name": "exec", "arguments": '{"cmd": "ls"}'}}],
            finish="tool_calls",
        )

    p = OpenAICompatProvider(model="MiniMax-M3", api_key="K", base_url="https://api.minimax.io/v1",
                             http_post=transport, provider_name="minimax")
    res = await p.chat([Message(role="user", content="chạy")], [ToolSchema("exec", "run", {})])
    assert res.stop_reason == "tool_use"
    assert res.tool_calls[0].name == "exec"
    assert res.tool_calls[0].args == {"cmd": "ls"}


async def test_auth_error_maps_to_provider_error() -> None:
    async def transport(url, headers, body):
        return 401, {"error": "invalid key"}

    p = OpenAICompatProvider(model="glm-5.2", api_key="bad", base_url="https://x", http_post=transport)
    with pytest.raises(ProviderError):
        await p.chat([Message(role="user", content="hi")], [])


async def test_rate_limit_maps() -> None:
    from yett.provider.base import FailReason

    async def transport(url, headers, body):
        return 429, {"error": "rate"}

    p = OpenAICompatProvider(model="glm-5.2", api_key="k", base_url="https://x", http_post=transport)
    with pytest.raises(ProviderError) as ei:
        await p.chat([Message(role="user", content="hi")], [])
    assert ei.value.reason == FailReason.RATE_LIMIT


def test_factory_builds_glm() -> None:
    secrets = InMemorySecretStore({"llm_key": "SECRET"})
    cfg = ProviderCfg(name="openai_compat", model="glm-5.2", api_key_secret="llm_key",
                      base_url="https://open.bigmodel.cn/api/paas/v4")
    p = build_provider(cfg, secrets)
    assert p.default_model() == "glm-5.2"


def test_factory_requires_base_url() -> None:
    secrets = InMemorySecretStore({"llm_key": "S"})
    cfg = ProviderCfg(name="openai_compat", model="glm-5.2", api_key_secret="llm_key")
    with pytest.raises(ValueError):
        build_provider(cfg, secrets)
