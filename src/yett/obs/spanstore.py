"""Span store (spec P0-P1 §3.6, §5.1). Buffer→flush SQLite; Tracer implement thật ở đây.

Timestamp truyền từ ngoài (spec §0: không dùng time ẩn để test deterministic).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from yett.obs.tracer import Span, SpanKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
  id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT,
  kind TEXT NOT NULL, name TEXT NOT NULL,
  start_ts REAL NOT NULL, end_ts REAL,
  session_key TEXT, attrs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_key, start_ts);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind, start_ts);
CREATE TABLE IF NOT EXISTS schema_version (v INTEGER);
"""


def _gen_id() -> str:
    return uuid.uuid4().hex


class SpanStore:
    """Tracer thật: giữ span trong bộ nhớ rồi flush xuống SQLite.

    Redact: attrs được lọc qua `redactor` (callable) trước khi ghi — bảo đảm
    không secret nào vào span (RG1-9). Mặc định identity nếu không truyền.
    """

    def __init__(self, db_path: str | Path, redactor=None, buffer_size: int = 100) -> None:
        self.db_path = str(db_path)
        self._redact = redactor or (lambda d: d)
        self._buffer: list[Span] = []
        self._buffer_size = buffer_size
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- Tracer interface ---
    def start_span(
        self,
        kind: SpanKind,
        name: str,
        *,
        start_ts: float,
        trace_id: str | None = None,
        parent_id: str | None = None,
        session_key: str | None = None,
    ) -> Span:
        return Span(
            id=_gen_id(),
            trace_id=trace_id or _gen_id(),
            kind=kind,
            name=name,
            start_ts=start_ts,
            parent_id=parent_id,
            session_key=session_key,
        )

    def end_span(self, span: Span, *, end_ts: float, **attrs: object) -> None:
        span.end_ts = end_ts
        span.attrs.update(attrs)
        span.attrs = self._redact(span.attrs)
        self._buffer.append(span)
        if len(self._buffer) >= self._buffer_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        rows = [
            (
                s.id, s.trace_id, s.parent_id, s.kind.name, s.name,
                s.start_ts, s.end_ts, s.session_key, json.dumps(s.attrs, ensure_ascii=False),
            )
            for s in self._buffer
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        self._conn.commit()
        self._buffer.clear()

    # --- query (cho CLI traces/usage) ---
    def get_trace(self, trace_id: str) -> list[dict]:
        self.flush()
        cur = self._conn.execute(
            "SELECT id,trace_id,parent_id,kind,name,start_ts,end_ts,session_key,attrs_json "
            "FROM spans WHERE trace_id=? ORDER BY start_ts", (trace_id,)
        )
        return [self._row(r) for r in cur.fetchall()]

    def list_traces(self, limit: int = 50) -> list[dict]:
        self.flush()
        cur = self._conn.execute(
            "SELECT id,trace_id,parent_id,kind,name,start_ts,end_ts,session_key,attrs_json "
            "FROM spans WHERE kind='AGENT' ORDER BY start_ts DESC LIMIT ?", (limit,)
        )
        return [self._row(r) for r in cur.fetchall()]

    @staticmethod
    def _row(r: tuple) -> dict:
        return {
            "id": r[0], "trace_id": r[1], "parent_id": r[2], "kind": r[3], "name": r[4],
            "start_ts": r[5], "end_ts": r[6], "session_key": r[7], "attrs": json.loads(r[8]),
        }

    def close(self) -> None:
        self.flush()
        self._conn.close()
