"""SQL classifier — L3 hardline (spec P2 §3.2). Chỉ cho SELECT/SHOW/EXPLAIN/DESCRIBE.

Yêu cầu người dùng: "tuyệt đối không ALTER/delete table/cột/dữ liệu nếu không được phép".
Parse bằng sqlglot; duyệt AST tìm mọi node ghi (kể cả trong CTE/subquery). Chặn:
multi-statement, CTE-DML, SELECT INTO, proc/CALL/EXEC. Parse fail → DENY (fail-closed).
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from yett.security.gate import Decision

# Node ghi dữ liệu/cấu trúc — bất kỳ cái nào xuất hiện → hardline deny.
_WRITE_EXPRESSIONS = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable if hasattr(exp, "TruncateTable") else exp.Drop,
    exp.Merge, exp.Grant if hasattr(exp, "Grant") else exp.Drop,
)
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Show, exp.Describe, exp.With, exp.Pragma)


def _normalize(sql: str) -> str:
    """Chuẩn hoá trước parse: bỏ comment splice để né obfuscation kiểu AL/**/TER."""
    # sqlglot tự xử lý comment, nhưng bỏ block comment giữa từ khoá cho chắc
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def _has_write(node: exp.Expression) -> bool:
    """Duyệt toàn bộ AST (kể cả CTE/subquery) tìm node ghi."""
    for sub in node.walk():
        n = sub[0] if isinstance(sub, tuple) else sub
        if isinstance(n, _WRITE_EXPRESSIONS):
            return True
        # SELECT ... INTO <table> (ghi bảng)
        if isinstance(n, exp.Select) and n.args.get("into") is not None:
            return True
        # Stored proc / CALL / EXEC
        if isinstance(n, exp.Command):
            name = (n.this or "").upper() if isinstance(n.this, str) else ""
            if name in {"CALL", "EXEC", "EXECUTE"}:
                return True
    return False


def classify_sql(sql: str, dialect: str = "postgres") -> Decision:
    norm = _normalize(sql)
    if not norm:
        return Decision("deny", "SQL rỗng", "SQL_EMPTY")
    try:
        stmts = [s for s in sqlglot.parse(norm, read=dialect) if s is not None]
    except Exception:
        return Decision("deny", "không parse được SQL — từ chối (fail-closed)", "SQL_UNPARSEABLE")
    if len(stmts) == 0:
        return Decision("deny", "không có statement hợp lệ", "SQL_EMPTY")
    if len(stmts) > 1:
        return Decision("deny", "multi-statement bị cấm (chỉ một câu SELECT mỗi lần)", "SQL_MULTI")
    stmt = stmts[0]
    # EXPLAIN/DESCRIBE/SHOW đôi khi được sqlglot parse thành Command → xử lý riêng.
    if isinstance(stmt, exp.Command):
        word = (stmt.this or "").upper() if isinstance(stmt.this, str) else ""
        if word in {"EXPLAIN"}:
            # EXPLAIN ANALYZE thực thi câu bên trong → phân loại lại phần bên trong (fail-closed).
            inner = re.sub(r"(?i)^\s*explain\s+(analyze\s+|verbose\s+)*", "", norm)
            return classify_sql(inner, dialect)
        if word in {"DESC", "DESCRIBE", "SHOW"}:
            return Decision("allow", "read-only metadata query", "SQL_READONLY")
        return Decision("deny", f"lệnh không được phép: {word or 'command'}", "SQL_NOT_READ")
    # bất kỳ node ghi nào → hardline
    if _has_write(stmt):
        return Decision(
            "deny",
            "HARDLINE: câu lệnh ghi/đổi cấu trúc (ALTER/DROP/DELETE/UPDATE/INSERT/TRUNCATE...) "
            "bị cấm. Muốn ghi phải approve tường minh từng câu qua db_write.",
            "SQL_WRITE_HARDLINE",
        )
    # root phải là loại đọc
    if not isinstance(stmt, _ALLOWED_ROOTS):
        return Decision("deny", f"loại câu lệnh không được phép đọc: {type(stmt).__name__}", "SQL_NOT_READ")
    return Decision("allow", "read-only query", "SQL_READONLY")
