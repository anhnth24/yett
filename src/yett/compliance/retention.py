"""Retention job (spec P3 §3.2). Xóa dữ liệu quá hạn theo config; mọi lần xóa ghi audit."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class RetentionCfg:
    traces_days: int = 90
    sessions_days: int = 365
    # audit không tự xóa (gov giữ lâu; xóa audit phải thủ công có phê duyệt)


def purge_old_spans(db_path: str | Path, *, now: float, keep_days: int, auditor: Callable[..., None] | None = None) -> int:
    """Xóa span cũ hơn keep_days. Trả số dòng xóa; ghi audit."""
    cutoff = now - keep_days * 86400
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("DELETE FROM spans WHERE start_ts < ?", (cutoff,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    if auditor:
        auditor(kind="retention", detail={"table": "spans", "deleted": n, "cutoff": cutoff})
    return n


def purge_old_messages(db_path: str | Path, *, now: float, keep_days: int, auditor: Callable[..., None] | None = None) -> int:
    cutoff = now - keep_days * 86400
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    if auditor:
        auditor(kind="retention", detail={"table": "messages", "deleted": n, "cutoff": cutoff})
    return n
