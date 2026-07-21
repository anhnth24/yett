"""Anthropic Messages API native provider adapter (spec P0-P1 §3.1).

Không thêm SDK — HTTP transport inject được (http_post) giống openai_compat; mặc định
urllib (stdlib). System prompt → top-level `system`; role=tool → user message với
tool_result blocks (gộp consecutive). Lỗi HTTP → ProviderError + FailReason cho failover.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable, Literal

from yett.provider.base import ChatResult, FailReason, Message, ToolCall, ToolSchema, Usage
from yett.provider.failover import ProviderError

_StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]

# http_post(url, headers, body_json) -> (status_code, response_dict)
HttpPost = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        provider_name: str = "anthropic",
        http_post: HttpPost | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
        timeout_sec: float = 120.0,
    ) -> None:
        self._model = model
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._name = provider_name
        self._post = http_post or _make_urllib_post(timeout_sec)
        self._max_tokens = max_tokens
        self._temp = temperature

    def name(self) -> str:
        return self._name

    def default_model(self) -> str:
        return self._model

    async def chat(
        self, messages: list[Message], tools: list[ToolSchema], *, stream: bool = False
    ) -> ChatResult:
        if stream:
            raise ProviderError(
                FailReason.BAD_REQUEST,
                "Anthropic streaming is not supported by the aggregated ChatResult transport",
            )
        system, api_messages = _to_anthropic_messages(messages)
        body: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
            "temperature": self._temp,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [_to_anthropic_tool(t) for t in tools]
        headers = {
            "x-api-key": self._key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        status, data = await self._post(f"{self._base}/messages", headers, body)
        if status != 200:
            raise ProviderError(_map_status(status, data), _safe_err(data, status, self._key))
        return _parse_response(data, self._model, self._key)


def _to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict]]:
    """Tách system → top-level string; chuyển role=tool → user/tool_result; gộp consecutive."""
    system_parts: list[str] = []
    out: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tools() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            out.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content,
                }
            )
            continue
        flush_tools()
        if m.role == "assistant" and m.tool_calls:
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.args,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": m.role, "content": m.content})
    flush_tools()
    return "\n\n".join(system_parts), out


def _to_anthropic_tool(t: ToolSchema) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.parameters or {"type": "object", "properties": {}},
    }


def _parse_response(data: dict, model: str, api_key: str) -> ChatResult:
    content = data.get("content")
    if not isinstance(content, list):
        raise ProviderError(
            FailReason.BAD_REQUEST,
            _redact(f"malformed Anthropic response: content missing/not list: {str(data)[:200]}", api_key),
        )
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "tool_use":
            raw_input = block.get("input")
            if isinstance(raw_input, dict):
                args = raw_input
            elif isinstance(raw_input, str):
                try:
                    args = json.loads(raw_input or "{}")
                except json.JSONDecodeError:
                    args = {}
            else:
                args = {}
            tool_calls.append(
                ToolCall(id=str(block.get("id") or ""), name=str(block.get("name") or ""), args=args)
            )
    usage_raw = data.get("usage") or {}
    if not isinstance(usage_raw, dict):
        usage_raw = {}
    usage = Usage(
        input_tokens=int(usage_raw.get("input_tokens") or 0),
        output_tokens=int(usage_raw.get("output_tokens") or 0),
        cache_read_tokens=int(usage_raw.get("cache_read_input_tokens") or 0),
    )
    stop_raw = data.get("stop_reason") or "end_turn"
    stop_map: dict[str, _StopReason] = {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
        "refusal": "refusal",
        "stop_sequence": "end_turn",
    }
    stop_reason: _StopReason = stop_map.get(str(stop_raw), "end_turn")
    if tool_calls and stop_reason == "end_turn":
        stop_reason = "tool_use"
    text = "".join(text_parts) if text_parts else None
    return ChatResult(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_reason,
        raw_model=str(data.get("model") or model),
    )


def _map_status(status: int, data: dict) -> FailReason:
    if status in (401, 403):
        return FailReason.AUTH
    if status == 429:
        return FailReason.RATE_LIMIT
    if status == 529:
        return FailReason.OVERLOADED
    if status == 408:
        return FailReason.TIMEOUT
    if status == 413 or _looks_like_overflow(data):
        return FailReason.CONTEXT_OVERFLOW
    if status == 400:
        err_type = _error_type(data)
        if err_type == "content_policy" or "content" in err_type and "filter" in err_type:
            return FailReason.CONTENT_FILTER
        return FailReason.BAD_REQUEST
    if status >= 500:
        return FailReason.SERVER_5XX
    return FailReason.NETWORK


def _error_type(data: dict) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("type") or "").lower()
    return ""


def _looks_like_overflow(data: dict) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    needles = (
        "prompt is too long",
        "context length",
        "context_length",
        "too many tokens",
        "max_tokens",
        "maximum context",
        "token limit",
    )
    # "max_tokens" alone is common in valid responses — only treat as overflow when error-shaped.
    if "error" not in blob and "invalid_request" not in blob:
        return False
    return any(n in blob for n in needles if n != "max_tokens") or (
        "max_tokens" in blob and ("exceed" in blob or "too" in blob or "limit" in blob)
    )


def _safe_err(data: dict, status: int, api_key: str) -> str:
    return _redact(f"HTTP {status}: {str(data)[:200]}", api_key)


def _redact(text: str, api_key: str) -> str:
    if api_key and api_key in text:
        return text.replace(api_key, "[REDACTED]")
    return text


def _make_urllib_post(timeout_sec: float) -> HttpPost:
    async def _urllib_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        """Transport mặc định (stdlib). Chạy trong thread để không chặn event loop."""
        import asyncio
        import socket
        import urllib.error
        import urllib.request

        def _do() -> tuple[int, dict]:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except TimeoutError as e:
                raise ProviderError(FailReason.TIMEOUT, "request timed out") from e
            except socket.timeout as e:
                raise ProviderError(FailReason.TIMEOUT, "request timed out") from e
            except urllib.error.HTTPError as e:
                try:
                    payload = json.loads(e.read().decode("utf-8"))
                except Exception:
                    payload = {"error": str(e)}
                return e.code, payload
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    raise ProviderError(FailReason.TIMEOUT, "request timed out") from e
                raise ProviderError(FailReason.NETWORK, _redact(str(e), headers.get("x-api-key", ""))) from e

        return await asyncio.to_thread(_do)

    return _urllib_post
