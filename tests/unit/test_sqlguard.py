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


# --- RT-5/RT-18: MySQL exec-comment /*! ... */ phải bị chặn BẤT KỂ dialect khai báo ---
# Trước fix: default dialect='postgres' coi /*! ... */ là comment thường và bỏ nội dung khi
# parse → câu "trông" vô hại lọt qua, trong khi MySQL thật lại THỰC THI nội dung bên trong.
EXEC_COMMENT_CASES = [
    "SELECT 1 /*!50000,(SELECT password FROM users)*/",
    "SELECT * FROM orders /*!UNION SELECT * FROM secrets*/",
    "SELECT 1/*!32302 UNION SELECT password FROM users*/",
]


@pytest.mark.parametrize("sql", EXEC_COMMENT_CASES)
@pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite", "tsql"])
def test_mysql_exec_comment_rejected_regardless_of_dialect(sql: str, dialect: str) -> None:
    dec = classify_sql(sql, dialect)
    assert dec.verdict == "deny", f"[{dialect}] /*! phải bị chặn: {sql}"
    assert dec.rule_id == "SQL_EXEC_COMMENT_HARDLINE"


def test_plain_select_still_allowed_after_exec_comment_fix() -> None:
    # Fix fail-closed không được làm lọt-deny câu SELECT thường (không chứa "/*!").
    assert classify_sql("SELECT * FROM orders WHERE id = 1").verdict == "allow"
    assert classify_sql("SELECT * FROM orders WHERE id = 1", "mysql").verdict == "allow"


def test_ordinary_block_comment_still_stripped() -> None:
    # "/**/" (không có "!") vẫn là comment thường, không bị fail-closed reject.
    assert classify_sql("SELECT 1 /* ordinary comment */").verdict == "allow"


def test_sqlserver_driver_alias_mapped_to_sqlglot_tsql() -> None:
    # DbProfile.driver="sqlserver" không phải tên dialect sqlglot hợp lệ ("tsql" mới đúng) —
    # trước đây gọi thẳng classify_sql(sql, "sqlserver") luôn fail-closed deny do parse lỗi
    # dialect. classify_sql phải tự map alias để câu SELECT hợp lệ trên SQL Server vẫn pass.
    dec = classify_sql("SELECT TOP 10 * FROM orders", "sqlserver")
    assert dec.verdict == "allow", dec.reason


def test_explain_without_space_does_not_recurse() -> None:
    # [L2] `EXPLAIN(SELECT 1)` (không space) trước đây làm regex strip `explain\s+` không bóc
    # được → inner==norm → đệ quy vô hạn (RecursionError + ~980 lần parse/log-flood). Nay luôn
    # trả Decision (không đệ quy); dạng lạ này fail-closed deny là chấp nhận được.
    dec = classify_sql("EXPLAIN(SELECT 1)", "postgres")
    assert dec.verdict in {"allow", "deny"}  # điểm mấu chốt: KHÔNG RecursionError
    # dạng EXPLAIN thường (có space) vẫn phân loại đúng phần bên trong → allow (không regress).
    assert classify_sql("EXPLAIN SELECT 1", "postgres").verdict == "allow"
