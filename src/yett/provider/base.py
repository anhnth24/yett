"""Provider interface (spec P0-P1 §3.1).

Một interface duy nhất cho mọi LLM provider — đổi model/provider bằng config,
không đụng core. Adapter cụ thể (anthropic, openai_compat) implement Protocol này.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    # P0-2: lượt assistant khi model gọi tool — PHẢI có trong context TRƯỚC tool_result
    # tương ứng (đúng thứ tự OpenAI/Anthropic). None với mọi role khác / assistant text-only.
    tool_calls: list[ToolCall] | None = None


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON schema


@dataclass(frozen=True)
class ChatResult:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "refusal"]
    raw_model: str


class FailReason(Enum):
    """9 lý do failover chuẩn (spec §3.1). CONTEXT_OVERFLOW KHÔNG failover —
    signal cho loop chạy compaction rồi retry."""

    RATE_LIMIT = 0
    TIMEOUT = 1
    SERVER_5XX = 2
    AUTH = 3
    BAD_REQUEST = 4
    CONTENT_FILTER = 5
    OVERLOADED = 6
    NETWORK = 7
    CONTEXT_OVERFLOW = 8


class Provider(Protocol):
    def name(self) -> str: ...
    def default_model(self) -> str: ...
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        stream: bool = False,
    ) -> ChatResult: ...
