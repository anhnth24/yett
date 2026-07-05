"""DbExecutor thật (spec P2 §3). L2 session read-only + chạy query.

sqlite dùng stdlib (chạy ngay); postgres/mysql/sqlserver lazy-import driver khi cần
(không bắt buộc cài nếu không dùng). DSN không bao giờ log.
"""

from __future__ import annotations

import sqlite3


class RealDbExecutor:
    async def query_readonly(self, dsn: str, driver: str, sql: str) -> list[dict]:
        if driver == "sqlite":
            return _sqlite_query(dsn, sql)
        if driver == "postgres":
            return await _pg_query(dsn, sql)
        if driver == "mysql":
            return _mysql_query(dsn, sql)
        raise ValueError(f"driver DB chưa hỗ trợ: {driver}")


def _sqlite_query(dsn: str, sql: str) -> list[dict]:
    # L2: mở read-only qua URI mode=ro (không cho ghi ở tầng driver).
    path = dsn.replace("sqlite://", "").replace("sqlite:", "") or dsn
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


async def _pg_query(dsn: str, sql: str) -> list[dict]:
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError("cần cài 'psycopg' để query PostgreSQL") from e
    # L2: session read-only
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute("SET default_transaction_read_only = on")
        cur = await conn.execute(sql)
        cols = [d.name for d in cur.description or []]
        rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]


def _mysql_query(dsn: str, sql: str) -> list[dict]:
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as e:
        raise RuntimeError("cần cài 'pymysql' để query MySQL") from e
    from urllib.parse import urlparse

    u = urlparse(dsn)
    conn = pymysql.connect(
        host=u.hostname, port=u.port or 3306, user=u.username, password=u.password,
        database=(u.path or "/").lstrip("/"), cursorclass=DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(sql)
            return list(cur.fetchall())
    finally:
        conn.close()
