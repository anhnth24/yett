"""Scheduler (spec P2 §7). 3 syntax at/every/cron; persist SQLite; chống overlap.

Timezone khai báo tường minh (không theo máy). Clock injectable để test deterministic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_jobs (
  id TEXT PRIMARY KEY, spec TEXT NOT NULL, tz TEXT NOT NULL,
  prompt TEXT NOT NULL, session_key TEXT NOT NULL,
  next_run REAL, running INTEGER DEFAULT 0, last_run REAL
);
"""


@dataclass
class CronJob:
    id: str
    spec: str  # "at:<iso>" | "every:<seconds>" | "cron:<expr>"
    tz: str
    prompt: str
    session_key: str


class CronStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, job: CronJob, *, next_run: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cron_jobs (id,spec,tz,prompt,session_key,next_run,running,last_run) "
            "VALUES (?,?,?,?,?,?,0,NULL)",
            (job.id, job.spec, job.tz, job.prompt, job.session_key, next_run),
        )
        self._conn.commit()

    def due(self, now: float) -> list[CronJob]:
        """Job tới hạn VÀ không đang chạy (chống overlap)."""
        cur = self._conn.execute(
            "SELECT id,spec,tz,prompt,session_key FROM cron_jobs "
            "WHERE next_run<=? AND running=0", (now,)
        )
        return [CronJob(*r) for r in cur.fetchall()]

    def mark_running(self, job_id: str) -> bool:
        """Đánh dấu đang chạy; trả False nếu đã chạy (overlap → skip)."""
        cur = self._conn.execute(
            "UPDATE cron_jobs SET running=1 WHERE id=? AND running=0", (job_id,)
        )
        self._conn.commit()
        return cur.rowcount == 1

    def finish(self, job_id: str, *, now: float, next_run: float | None) -> None:
        self._conn.execute(
            "UPDATE cron_jobs SET running=0, last_run=?, next_run=? WHERE id=?",
            (now, next_run, job_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def compute_next(spec: str, now: float, tz: str) -> float | None:
    """Tính lần chạy kế. at → None sau khi chạy; every:N → now+N; cron → dùng croniter nếu có."""
    kind, _, val = spec.partition(":")
    if kind == "at":
        return None
    if kind == "every":
        return now + float(val)
    if kind == "cron":
        try:
            from croniter import croniter  # optional
            from datetime import datetime, timezone as _tz

            base = datetime.fromtimestamp(now, tz=_tz.utc)
            return croniter(val, base).get_next(float)
        except Exception:
            return now + 3600.0  # fallback: mỗi giờ
    return None
