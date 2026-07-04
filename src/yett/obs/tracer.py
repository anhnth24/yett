"""Tracer (spec P0-P1 §3.6).

First-class từ commit đầu (không bolt-on). Mọi model/tool/hook call có span +
correlation id, replay được một turn. Span KHÔNG chứa secret (qua redact trước khi ghi).
Parent-child cho subagent (P3) dùng lại cấu trúc này.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class SpanKind(Enum):
    AGENT = 0
    LLM_CALL = 1
    TOOL_CALL = 2
    EMBEDDING = 3
    EVENT = 4


@dataclass
class Span:
    id: str
    trace_id: str
    kind: SpanKind
    name: str
    start_ts: float
    parent_id: str | None = None
    end_ts: float | None = None
    session_key: str | None = None
    attrs: dict = field(default_factory=dict)  # usage, cost, verdict... (đã redact)


class Tracer(Protocol):
    """Interface Tracer. Thân thật (buffer→flush SQLite) implement ở spanstore.py (WP1.6).

    Ghi chú: start_ts/end_ts nhận từ bên ngoài (spec §0: không dùng time ẩn trong
    logic quyết định; timestamp truyền vào để test deterministic).
    """

    def start_span(
        self, kind: SpanKind, name: str, *, start_ts: float, parent_id: str | None = None
    ) -> Span: ...

    def end_span(self, span: Span, *, end_ts: float, **attrs: object) -> None: ...
