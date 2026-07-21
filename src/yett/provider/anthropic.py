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

# http_post(url, headers, body_json) -> (status_code, decoded_response)
HttpPost = Callable[[str, dict, dict], Awaitable[tuple[int, object]]]

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
        temperature: float | None = None,
        timeout_sec: float = 120.0,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if temperature is not None and not 0.0 <= temperature <= 1.0:
            raise ValueError("temperature must be between 0 and 1")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
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
        }
        # Current adaptive-thinking models reject non-default sampling parameters and
        # require thinking blocks to be round-tripped during tool use.  The provider
        # contract cannot retain opaque thinking blocks, so disable thinking explicitly.
        if _needs_thinking_disabled(self._model):
            body["thinking"] = {"type": "disabled"}
        if self._temp is not None:
            body["temperature"] = self._temp
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [_to_anthropic_tool(t) for t in tools]
        headers = {
            "x-api-key": self._key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        try:
            json.dumps(body, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProviderError(FailReason.BAD_REQUEST, f"request is not valid JSON: {exc}") from None
        try:
            status, data = await self._post(f"{self._base}/messages", headers, body)
        except ProviderError as exc:
            safe = _redact(str(exc), self._key)
            if safe != str(exc):
                raise ProviderError(exc.reason, safe) from None
            raise
        except TimeoutError:
            raise ProviderError(FailReason.TIMEOUT, "request timed out") from None
        except OSError as exc:
            raise ProviderError(FailReason.NETWORK, _redact(str(exc), self._key)) from None
        except Exception:
            # Injected proxy/test transports are not trusted to produce secret-safe exception
            # strings (they may include the full headers mapping).  Canonicalize instead of
            # letting an implementation-specific exception abort the agent loop.
            raise ProviderError(FailReason.NETWORK, "Anthropic transport failed") from None
        if status != 200:
            raise ProviderError(_map_status(status, data), _safe_err(data, status, self._key))
        return _parse_response(data, self._model, self._name, self._key)


def _to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict]]:
    """Tách system → top-level string; chuyển role=tool → user/tool_result; gộp consecutive."""
    system_parts: list[str] = []
    out: list[dict] = []
    pending_tool_results: list[dict] = []
    expected_tool_results: set[str] | None = None

    def flush_tools() -> None:
        nonlocal expected_tool_results, pending_tool_results
        if expected_tool_results:
            raise ProviderError(
                FailReason.BAD_REQUEST,
                f"missing tool results for: {', '.join(sorted(expected_tool_results))}",
            )
        if pending_tool_results:
            out.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []
        expected_tool_results = None

    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == "tool":
            if not m.tool_call_id:
                raise ProviderError(FailReason.BAD_REQUEST, "tool result is missing tool_call_id")
            if expected_tool_results is None or m.tool_call_id not in expected_tool_results:
                raise ProviderError(
                    FailReason.BAD_REQUEST,
                    f"tool result {m.tool_call_id!r} has no matching preceding tool call",
                )
            expected_tool_results.remove(m.tool_call_id)
            block: dict = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content,
            }
            if m.tool_result_is_error:
                block["is_error"] = True
            pending_tool_results.append(
                block
            )
            continue
        flush_tools()
        if m.role == "assistant" and m.tool_calls:
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                if not tc.id or not tc.name or not isinstance(tc.args, dict):
                    raise ProviderError(
                        FailReason.BAD_REQUEST, "assistant tool call requires id, name, and object args"
                    )
                if expected_tool_results is None:
                    expected_tool_results = set()
                if tc.id in expected_tool_results:
                    raise ProviderError(FailReason.BAD_REQUEST, f"duplicate tool call id: {tc.id}")
                expected_tool_results.add(tc.id)
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


def _parse_response(data: object, model: str, provider_name: str, api_key: str) -> ChatResult:
    if not isinstance(data, dict):
        raise _malformed("response is not an object", data, api_key)
    if data.get("type") != "message" or data.get("role") != "assistant":
        raise _malformed("type/role is not message/assistant", data, api_key)
    content = data.get("content")
    if not isinstance(content, list):
        raise _malformed("content missing/not list", data, api_key)
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    tool_call_ids: set[str] = set()
    has_thinking = False
    for block in content:
        if not isinstance(block, dict):
            raise _malformed("content block is not an object", data, api_key)
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise _malformed("text block has non-string text", data, api_key)
            text_parts.append(text)
        elif btype == "tool_use":
            raw_input = block.get("input")
            call_id = block.get("id")
            name = block.get("name")
            if not isinstance(raw_input, dict):
                raise _malformed("tool_use input is not an object", data, api_key)
            if not isinstance(call_id, str) or not call_id:
                raise _malformed("tool_use id missing/not string", data, api_key)
            if not isinstance(name, str) or not name:
                raise _malformed("tool_use name missing/not string", data, api_key)
            if call_id in tool_call_ids:
                raise _malformed(f"duplicate tool_use id {call_id!r}", data, api_key)
            tool_call_ids.add(call_id)
            tool_calls.append(
                ToolCall(id=call_id, name=name, args=raw_input)
            )
        elif btype in ("thinking", "redacted_thinking"):
            has_thinking = True
        else:
            raise _malformed(f"unsupported content block type {btype!r}", data, api_key)
    usage_raw = data.get("usage")
    if not isinstance(usage_raw, dict):
        raise _malformed("usage missing/not object", data, api_key)
    usage = Usage(
        input_tokens=_token_count(usage_raw, "input_tokens", required=True, data=data, api_key=api_key),
        output_tokens=_token_count(
            usage_raw, "output_tokens", required=True, data=data, api_key=api_key
        ),
        cache_read_tokens=_token_count(
            usage_raw, "cache_read_input_tokens", required=False, data=data, api_key=api_key
        ),
    )
    stop_raw = data.get("stop_reason")
    stop_map: dict[str, _StopReason] = {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
        "refusal": "refusal",
        "stop_sequence": "end_turn",
    }
    if not isinstance(stop_raw, str) or stop_raw not in stop_map:
        raise _malformed(f"unsupported stop_reason {stop_raw!r}", data, api_key)
    stop_reason = stop_map[stop_raw]
    if bool(tool_calls) != (stop_reason == "tool_use"):
        raise _malformed("tool_use blocks and stop_reason are inconsistent", data, api_key)
    if has_thinking and tool_calls:
        raise _malformed("thinking blocks cannot be round-tripped during tool use", data, api_key)
    text = "".join(text_parts) if text_parts else None
    raw_model = data.get("model")
    if raw_model is not None and (not isinstance(raw_model, str) or not raw_model):
        raise _malformed("model is not a non-empty string", data, api_key)
    return ChatResult(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_reason,
        raw_model=raw_model or model,
        provider_name=provider_name,
    )


def _token_count(
    usage: dict, field: str, *, required: bool, data: dict, api_key: str
) -> int:
    value = usage.get(field)
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _malformed(f"usage.{field} missing/not a non-negative integer", data, api_key)
    return value


def _malformed(detail: str, data: object, api_key: str) -> ProviderError:
    return ProviderError(
        FailReason.SERVER_5XX,
        f"malformed Anthropic response ({detail}): {_preview(data, api_key)}",
    )


def _needs_thinking_disabled(model: str) -> bool:
    return model in {
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
    }


def _map_status(status: int, data: object) -> FailReason:
    if status in (401, 402, 403):
        return FailReason.AUTH
    if status == 429:
        return FailReason.RATE_LIMIT
    if status == 529:
        return FailReason.OVERLOADED
    if status in (408, 504):
        return FailReason.TIMEOUT
    if status == 413 or _looks_like_overflow(data):
        return FailReason.CONTEXT_OVERFLOW
    if 400 <= status < 500:
        err_type = _error_type(data)
        if err_type == "content_policy" or "content" in err_type and "filter" in err_type:
            return FailReason.CONTENT_FILTER
        return FailReason.BAD_REQUEST
    if status >= 500:
        return FailReason.SERVER_5XX
    return FailReason.NETWORK


def _error_type(data: object) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("type") or "").lower()
    return ""


def _looks_like_overflow(data: object) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    needles = (
        "prompt is too long",
        "context length",
        "context_length",
        "maximum context",
        "input is too long",
        "input tokens",
    )
    # A max_tokens parameter above a model's output cap is a plain bad request, not input
    # context overflow; compaction cannot repair it.
    if "error" not in blob and "invalid_request" not in blob:
        return False
    return any(n in blob for n in needles)


def _safe_err(data: object, status: int, api_key: str) -> str:
    return f"HTTP {status}: {_preview(data, api_key)}"


def _preview(data: object, api_key: str) -> str:
    # Redact before truncating so a key crossing the preview boundary cannot leak a prefix.
    return _redact(str(data), api_key)[:200]


def _redact(text: str, api_key: str) -> str:
    if api_key and api_key in text:
        return text.replace(api_key, "[REDACTED]")
    return text


def _make_urllib_post(timeout_sec: float) -> HttpPost:
    async def _urllib_post(url: str, headers: dict, body: dict) -> tuple[int, object]:
        """Transport mặc định (stdlib). Chạy trong thread để không chặn event loop."""
        import asyncio
        import http.client
        import socket
        import urllib.error
        import urllib.request

        def _do() -> tuple[int, object]:
            req = urllib.request.Request(
                url,
                data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    try:
                        payload = json.loads(resp.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise ProviderError(
                            FailReason.SERVER_5XX, "malformed JSON in Anthropic response"
                        ) from None
                    return resp.status, payload
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
            except (http.client.HTTPException, OSError) as e:
                raise ProviderError(
                    FailReason.NETWORK, _redact(str(e), headers.get("x-api-key", ""))
                ) from e

        return await asyncio.to_thread(_do)

    return _urllib_post
