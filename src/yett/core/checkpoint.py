"""Checkpoint store (spec P0-P1 §5.2, P1.2.4). Persist trạng thái vòng lặp → resume.

Bất biến: resume KHÔNG chạy lại tool đã có side-effect. RT-2: lưu ĐẦY ĐỦ lịch sử message
(assistant tool_use + nội dung tool_result) — không chỉ id — để `AgentLoop` rebuild lại
`Context` khi resume mà KHÔNG cần hỏi lại model từ đầu (id do provider cấp non-deterministic
giữa các lần gọi nên không thể dùng làm khoá idempotency qua resume). Idempotency khi resume
dựa trên chữ ký ổn định `tool_name + hash(args)` (`completed_sigs`, xem `core.context`).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from yett.provider.base import Message, ToolCall

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
  session_key TEXT NOT NULL, turn_id TEXT NOT NULL,
  iteration INTEGER, state_json TEXT NOT NULL,
  status TEXT NOT NULL, updated_ts REAL,
  PRIMARY KEY (session_key, turn_id)
);
"""


def serialize_messages(messages: list[Message]) -> list[dict]:
    """Chuyển `list[Message]` (kể cả assistant tool_use) thành dict JSON-safe để lưu
    checkpoint (RT-2)."""
    out: list[dict] = []
    for m in messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.tool_result_is_error:
            d["tool_result_is_error"] = True
        if m.tool_calls:
            d["tool_calls"] = [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in m.tool_calls]
        out.append(d)
    return out


def deserialize_messages(data: list[dict]) -> list[Message]:
    """Ngược lại `serialize_messages` — dùng để rebuild `Context` khi resume (RT-2)."""
    out: list[Message] = []
    for d in data:
        raw_tc = d.get("tool_calls")
        tool_calls = (
            [ToolCall(id=t["id"], name=t["name"], args=t["args"]) for t in raw_tc] if raw_tc else None
        )
        out.append(
            Message(
                role=d["role"],
                content=d["content"],
                tool_call_id=d.get("tool_call_id"),
                tool_calls=tool_calls,
                tool_result_is_error=bool(d.get("tool_result_is_error", False)),
            )
        )
    return out


class CheckpointStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(
        self, session_key: str, turn_id: str, iteration: int, state: dict, *, ts: float, status: str = "running"
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?)",
            (session_key, turn_id, iteration, json.dumps(state, ensure_ascii=False), status, ts),
        )
        self._conn.commit()

    def mark(self, session_key: str, turn_id: str, status: str, *, ts: float) -> None:
        self._conn.execute(
            "UPDATE checkpoints SET status=?, updated_ts=? WHERE session_key=? AND turn_id=?",
            (status, ts, session_key, turn_id),
        )
        self._conn.commit()

    def load(self, session_key: str, turn_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT iteration, state_json, status FROM checkpoints WHERE session_key=? AND turn_id=?",
            (session_key, turn_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"iteration": row[0], "state": json.loads(row[1]), "status": row[2]}

    def close(self) -> None:
        self._conn.close()
