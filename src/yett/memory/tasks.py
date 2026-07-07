"""Task/Goal store (lớp trợ lý cá nhân). SQLite — việc cần làm & mục tiêu của người dùng.

yett dùng để 'nhớ đang làm dở gì' theo VIỆC (không chỉ theo hội thoại): thêm/liệt kê/cập
nhật/hoàn thành. Là nền cho briefing chủ động ('hôm nay có 3 việc, 1 đến hạn') và cho câu
hỏi 'tôi đang làm gì'. due lưu dạng ISO 'YYYY-MM-DD' (so sánh theo chuỗi, không dính tz).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STATUSES = ("todo", "doing", "done")
PRIORITIES = ("low", "normal", "high")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  project TEXT,
  status TEXT NOT NULL DEFAULT 'todo',
  priority TEXT NOT NULL DEFAULT 'normal',
  due TEXT,                       -- ISO 'YYYY-MM-DD' hoặc NULL
  notes TEXT,
  created_ts REAL NOT NULL,
  updated_ts REAL NOT NULL,
  done_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_task_status ON tasks (status, updated_ts);
"""

_COLS = ["id", "title", "project", "status", "priority", "due", "notes",
         "created_ts", "updated_ts", "done_ts"]


@dataclass
class Task:
    id: int
    title: str
    project: str | None
    status: str
    priority: str
    due: str | None
    notes: str | None
    created_ts: float
    updated_ts: float
    done_ts: float | None

    def as_dict(self) -> dict:
        return {c: getattr(self, c) for c in _COLS}


class TaskStore:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float]) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = clock

    def add(self, title: str, *, project: str | None = None, priority: str = "normal",
            due: str | None = None, notes: str | None = None) -> Task:
        if not title.strip():
            raise ValueError("title rỗng")
        if priority not in PRIORITIES:
            raise ValueError(f"priority phải thuộc {PRIORITIES}")
        now = self._clock()
        cur = self._conn.execute(
            "INSERT INTO tasks (title, project, status, priority, due, notes, created_ts, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (title.strip(), project, "todo", priority, due, notes, now, now),
        )
        self._conn.commit()
        got = self.get(cur.lastrowid or 0)
        assert got is not None
        return got

    def get(self, task_id: int) -> Task | None:
        cur = self._conn.execute(
            f"SELECT {','.join(_COLS)} FROM tasks WHERE id=?", (task_id,)
        )
        row = cur.fetchone()
        return Task(*row) if row else None

    def list_tasks(self, *, status: str | None = None, project: str | None = None) -> list[Task]:
        q = f"SELECT {','.join(_COLS)} FROM tasks"
        conds, args = [], []
        if status:
            conds.append("status=?"); args.append(status)
        if project:
            conds.append("project=?"); args.append(project)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        # todo/doing lên trước done; trong nhóm: priority cao trước, rồi due sớm trước.
        q += (" ORDER BY status='done', "
              "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
              "due IS NULL, due, updated_ts DESC")
        return [Task(*r) for r in self._conn.execute(q, args).fetchall()]

    def update(self, task_id: int, **fields) -> Task | None:
        allowed = {"title", "project", "status", "priority", "due", "notes"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "status" in sets and sets["status"] not in STATUSES:
            raise ValueError(f"status phải thuộc {STATUSES}")
        if "priority" in sets and sets["priority"] not in PRIORITIES:
            raise ValueError(f"priority phải thuộc {PRIORITIES}")
        if not sets:
            return self.get(task_id)
        now = self._clock()
        assigns = ", ".join(f"{k}=?" for k in sets) + ", updated_ts=?"
        args = list(sets.values()) + [now]
        if sets.get("status") == "done":
            assigns += ", done_ts=?"; args.append(now)
        args.append(task_id)
        self._conn.execute(f"UPDATE tasks SET {assigns} WHERE id=?", args)
        self._conn.commit()
        return self.get(task_id)

    def delete(self, task_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def open_tasks(self) -> list[Task]:
        """Việc chưa xong (todo + doing)."""
        return [t for t in self.list_tasks() if t.status != "done"]

    def due_on_or_before(self, iso_date: str) -> list[Task]:
        """Việc chưa xong có due <= ngày cho trước (ISO 'YYYY-MM-DD')."""
        return [t for t in self.open_tasks() if t.due and t.due <= iso_date]

    def close(self) -> None:
        self._conn.close()
