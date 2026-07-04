"""AG-6: hardline DB — chỉ SELECT/SHOW/EXPLAIN; chặn mọi DML/DDL + né tránh (RG2-8)."""

from __future__ import annotations

import pytest

from yett.tools.db.sqlguard import classify_sql

WRITE_CASES = [
    "UPDATE orders SET status='x' WHERE id=1",
    "DELETE FROM orders WHERE id=1",
    "delete from orders",
    "ALTER TABLE orders ADD COLUMN foo int",
    "DROP TABLE orders",
    "TRUNCATE TABLE orders",
    "INSERT INTO orders (id) VALUES (1)",
    "CREATE TABLE x (id int)",
    "SELECT * FROM orders; DROP TABLE orders",          # multi-statement
    "WITH w AS (DELETE FROM orders RETURNING *) SELECT * FROM w",  # CTE-DML
    "SELECT * INTO backup FROM orders",                 # SELECT INTO
    "AL/**/TER TABLE orders ADD c int",                 # comment splice obfuscation
    "UPDATE/**/orders SET x=1",
    "  update   orders  set x=1 ",                       # spacing
    "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.a=s.a",
]


@pytest.mark.parametrize("sql", WRITE_CASES)
def test_write_blocked(sql: str) -> None:
    dec = classify_sql(sql)
    assert dec.verdict == "deny", f"KHÔNG chặn: {sql}"
    assert dec.rule_id in ("SQL_WRITE_HARDLINE", "SQL_MULTI", "SQL_UNPARSEABLE", "SQL_NOT_READ")


READ_CASES = [
    "SELECT * FROM orders WHERE id = 123",
    "select id, status from orders where created_at > now() - interval '1 day'",
    "SELECT count(*) FROM orders",
    "WITH recent AS (SELECT * FROM orders WHERE id > 100) SELECT * FROM recent",
    "EXPLAIN SELECT * FROM orders",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id",
]


@pytest.mark.parametrize("sql", READ_CASES)
def test_read_allowed(sql: str) -> None:
    dec = classify_sql(sql)
    assert dec.verdict == "allow", f"read bị chặn nhầm: {sql} -> {dec.reason}"


def test_unparseable_fail_closed() -> None:
    dec = classify_sql("SELECT FROM WHERE )(")
    assert dec.verdict == "deny"


def test_empty_denied() -> None:
    assert classify_sql("   ").verdict == "deny"


def test_subquery_with_delete_blocked() -> None:
    # DML giấu trong subquery
    dec = classify_sql("SELECT * FROM (DELETE FROM orders RETURNING *) x")
    assert dec.verdict == "deny"


def test_explain_analyze_write_blocked() -> None:
    # EXPLAIN ANALYZE thực thi câu bên trong → phải chặn nếu bên trong là write.
    dec = classify_sql("EXPLAIN ANALYZE DELETE FROM orders WHERE id=1")
    assert dec.verdict == "deny"


def test_explain_select_allowed() -> None:
    assert classify_sql("EXPLAIN ANALYZE SELECT * FROM orders").verdict == "allow"
