"""Cross-session memory store (spec P2 WP2.3). SQLite FTS5 + trigram (schema kiểu Hermes).

session_search trả message GỐC (không LLM summarization — bài học từ README Hermes nói quá).
Trigram tokenizer cho substring/CJK — tiếng Việt có dấu hưởng lợi.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS sessions (
  key TEXT PRIMARY KEY, created_ts REAL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_version (v INTEGER);
"""


def _has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SCHEMA_BASE)
        self._fts = _has_fts5(self._conn)
        if self._fts:
            # trigram tokenizer: tốt cho substring + tiếng Việt/CJK
            self._conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(content, session_key UNINDEXED, content_rowid UNINDEXED,
                           tokenize='trigram');
                """
            )
        self._conn.commit()

    def add_message(self, session_key: str, role: str, content: str, *, ts: float) -> int:
        cur = self._conn.execute(
            "INSERT INTO messages (session_key, role, content, ts) VALUES (?,?,?,?)",
            (session_key, role, content, ts),
        )
        rowid = cur.lastrowid or 0
        if self._fts:
            self._conn.execute(
                "INSERT INTO messages_fts (rowid, content, session_key, content_rowid) "
                "VALUES (?,?,?,?)",
                (rowid, content, session_key, rowid),
            )
        self._conn.commit()
        return rowid

    def search(self, query: str, *, limit: int = 10) -> list[dict]:
        """Trả message GỐC khớp query. Dùng FTS5 nếu có, else LIKE fallback."""
        if self._fts:
            cur = self._conn.execute(
                "SELECT m.session_key, m.role, m.content, m.ts FROM messages_fts f "
                "JOIN messages m ON m.id = f.rowid WHERE messages_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (_fts_quote(query), limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT session_key, role, content, ts FROM messages "
                "WHERE content LIKE ? ORDER BY ts DESC LIMIT ?",
                (f"%{query}%", limit),
            )
        return [
            {"session_key": r[0], "role": r[1], "content": r[2], "ts": r[3]}
            for r in cur.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()


def _fts_quote(q: str) -> str:
    # bọc trong ngoặc kép để trigram match cụm, escape dấu nháy
    return '"' + q.replace('"', '""') + '"'
