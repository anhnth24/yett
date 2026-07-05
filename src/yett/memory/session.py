"""Session history (nhớ hội thoại xuyên turn — spec P0-P1 §3.8 mở rộng).

Lưu từng lượt (user + assistant) theo session_key vào SQLite. Turn sau nạp lại các
lượt gần nhất trong token budget → agent nhớ "đang làm dở gì". Provider-agnostic:
đổi provider không mất lịch sử (lịch sử nằm ở harness, không ở provider).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yett.core.context import estimate_tokens
from yett.provider.base import Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_msgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session ON session_msgs (session_key, id);
"""


class SessionStore:
    """Lưu/nạp lịch sử hội thoại theo session_key."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def append(self, session_key: str, role: str, content: str, *, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO session_msgs (session_key, role, content, ts) VALUES (?,?,?,?)",
            (session_key, role, content, ts),
        )
        self._conn.commit()

    def record_turn(self, session_key: str, user: str, assistant: str, *, ts: float) -> None:
        """Ghi một lượt hoàn chỉnh (câu người dùng + trả lời agent)."""
        self.append(session_key, "user", user, ts=ts)
        self.append(session_key, "assistant", assistant, ts=ts)

    def history(self, session_key: str, *, token_budget: int) -> list[Message]:
        """Các lượt gần nhất, cắt từ cũ để không vượt token_budget (giữ nguyên thứ tự thời gian).

        Chỉ nạp user/assistant (không nạp tool result cũ — tránh phình context;
        tool result chỉ có ý nghĩa trong turn tạo ra nó)."""
        cur = self._conn.execute(
            "SELECT role, content FROM session_msgs WHERE session_key=? ORDER BY id DESC",
            (session_key,),
        )
        picked: list[Message] = []
        used = 0
        for role, content in cur:
            t = estimate_tokens(content)
            if used + t > token_budget:
                break
            picked.append(Message(role=role, content=content))
            used += t
        picked.reverse()  # DESC → trả về đúng thứ tự thời gian (cũ trước)
        return picked

    def clear(self, session_key: str) -> None:
        self._conn.execute("DELETE FROM session_msgs WHERE session_key=?", (session_key,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
