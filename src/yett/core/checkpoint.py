"""Checkpoint store (spec P0-P1 §5.2, P1.2.4). Persist trạng thái vòng lặp → resume.

Bất biến: resume KHÔNG chạy lại tool đã có side-effect. Ta lưu tập tool_call_id đã
hoàn thành; khi resume, các tool_call này bị bỏ qua (idempotency).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
  session_key TEXT NOT NULL, turn_id TEXT NOT NULL,
  iteration INTEGER, state_json TEXT NOT NULL,
  status TEXT NOT NULL, updated_ts REAL,
  PRIMARY KEY (session_key, turn_id)
);
"""


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
