"""OpenAI-compatible provider adapter (spec P0-P1 §3.1).

Chạy được với mọi endpoint OpenAI-compatible: MiniMax, GLM (Zhipu), OpenAI, OpenRouter,
Groq, DeepSeek, hoặc bất kỳ server tương thích. Chỉ khác base_url + model + key.

HTTP transport inject được (http_post) → test offline không gọi mạng. Mặc định dùng
urllib (stdlib, không thêm dep). Lỗi HTTP → ProviderError với FailReason để failover xử lý.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable, Literal

from yett.provider.base import ChatResult, FailReason, Message, ToolCall, ToolSchema, Usage
from yett.provider.failover import ProviderError

_StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]

# http_post(url, headers, body_json) -> (status_code, response_dict)
HttpPost = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        provider_name: str = "openai_compat",
        http_post: HttpPost | None = None,
        temperature: float = 0.7,
    ) -> None:
        self._model = model
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._name = provider_name
        self._post = http_post or _urllib_post
        self._temp = temperature

    def name(self) -> str:
        return self._name

    def default_model(self) -> str:
        return self._model

    async def chat(
        self, messages: list[Message], tools: list[ToolSchema], *, stream: bool = False
    ) -> ChatResult:
        body: dict = {
            "model": self._model,
            "messages": [_to_openai_msg(m) for m in messages],
            "temperature": self._temp,
        }
        if tools:
            body["tools"] = [_to_openai_tool(t) for t in tools]
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        status, data = await self._post(f"{self._base}/chat/completions", headers, body)
        if status != 200:
            raise ProviderError(_map_status(status), f"HTTP {status}: {str(data)[:200]}")
        return _parse_response(data, self._model, self._name)


def _to_openai_msg(m: Message) -> dict:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content}
    if m.role == "assistant" and m.tool_calls:
        # P0-2: assistant tool_use — content nullable theo chuẩn OpenAI khi model không kèm
        # text (chỉ gọi tool).
        return {
            "role": "assistant",
            "content": m.content or None,
            "tool_calls": [_to_openai_tool_call(tc) for tc in m.tool_calls],
        }
    return {"role": m.role, "content": m.content}


def _to_openai_tool_call(tc: ToolCall) -> dict:
    return {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)},
    }


def _to_openai_tool(t: ToolSchema) -> dict:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
    }


def _parse_response(data: dict, model: str, provider_name: str = "") -> ChatResult:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    text = msg.get("content")
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), args=args))
    usage_raw = data.get("usage", {}) or {}
    usage = Usage(
        input_tokens=usage_raw.get("prompt_tokens", 0),
        output_tokens=usage_raw.get("completion_tokens", 0),
        cache_read_tokens=(usage_raw.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0),
    )
    finish = choice.get("finish_reason", "stop")
    stop_map: dict[str, _StopReason] = {
        "stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
        "content_filter": "refusal",
    }
    stop_reason: _StopReason = stop_map.get(finish, "end_turn")
    return ChatResult(
        text=text, tool_calls=tool_calls, usage=usage,
        stop_reason=stop_reason, raw_model=data.get("model", model), provider_name=provider_name,
    )


def _map_status(status: int) -> FailReason:
    if status == 401 or status == 403:
        return FailReason.AUTH
    if status == 429:
        return FailReason.RATE_LIMIT
    if status == 400:
        return FailReason.BAD_REQUEST
    if status >= 500:
        return FailReason.SERVER_5XX
    return FailReason.NETWORK


async def _urllib_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
    """Transport mặc định (stdlib). Chạy trong thread để không chặn event loop."""
    import asyncio
    import urllib.error
    import urllib.request

    def _do() -> tuple[int, dict]:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {"error": str(e)}
            return e.code, payload
        except urllib.error.URLError as e:
            raise ProviderError(FailReason.NETWORK, str(e)) from e

    return await asyncio.to_thread(_do)
