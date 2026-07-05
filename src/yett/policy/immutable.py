"""Immutable core (spec P3 §2.3). Hardline không rule config nào đảo được + identity read-only.

check() trả deny cho: lệnh phá hoại (denylist), SSH xóa file, DB write, và mọi hành vi
chạm ghi vào vùng immutable (policy file, identity file). Subagent kế thừa, không nới.
"""

from __future__ import annotations

from pathlib import Path

from yett.security import denylist
from yett.security.cmdguard import CmdClass, classify as classify_cmd
from yett.security.gate import Decision
from yett.tools.db.sqlguard import classify_sql

# [RT-5/RT-18] Dialect sqlglot được hỗ trợ — dùng khi Gate chưa biết driver thật của DB
# profile (args ở lớp này là raw args từ model, chưa resolve qua db_query.py).
_SQL_DIALECTS = ("postgres", "mysql", "tsql", "sqlite")


class ImmutableCore:
    def __init__(self, protected_paths: list[Path] | None = None) -> None:
        self._protected = [p.resolve() for p in (protected_paths or [])]

    def check(self, tool: str, args: dict) -> Decision | None:
        """Trả Decision(deny) nếu chạm hardline; None nếu không."""
        # exec/ssh: hardline xóa file (cmdguard) + phá hoại (denylist)
        if tool in ("exec", "ssh_exec"):
            cmd = str(args.get("cmd", ""))
            cls, why = classify_cmd(cmd)
            if cls == CmdClass.DELETE_FILE:
                return Decision("deny", f"HARDLINE xóa file OS ({why})", "SSH_DELETE_HARDLINE")
            if hit := denylist.check_exec(cmd):
                return hit
        # db: hardline write
        if tool == "db_query":
            dec = self._classify_db_query(str(args.get("sql", "")), args.get("driver"))
            if dec.verdict != "allow":
                return dec
        # ghi vào vùng immutable (policy/identity)
        if tool in ("write_file", "exec"):
            target = str(args.get("path", "")) or str(args.get("cmd", ""))
            for p in self._protected:
                if str(p) in target:
                    return Decision(
                        "deny", "HARDLINE: không được sửa policy/identity (immutable core)",
                        "IMMUTABLE_WRITE",
                    )
        return None

    @staticmethod
    def _classify_db_query(sql: str, driver: object) -> Decision:
        """[RT-5/RT-18] Phân loại SQL cho db_query ở lớp immutable-core.

        TRƯỚC: gọi `classify_sql(sql)` mặc định dialect='postgres' — sai lệch với driver thật
        (vd MySQL) khiến vài payload chỉ nguy hiểm ở dialect thật lọt qua vì postgres phân loại
        khác đi. `db_query.py` tự nó gọi `classify_sql(sql, prof.driver)` đúng driver đã resolve
        từ profile; nhưng Gate/PolicyEngine chạy TRƯỚC khi tool.run() resolve profile nên args ở
        đây chỉ có 'sql' thô từ model, chưa có driver thật.

        Vì vậy: nếu caller (hiện tại hoặc tương lai) truyền sẵn `driver` trong args → dùng đúng
        dialect đó. Nếu không có (mọi call-site hiện tại qua Gate) → KHÔNG mặc định một dialect
        duy nhất — fail-closed bằng cách phân loại qua TẤT CẢ dialect được hỗ trợ và deny nếu
        bất kỳ dialect nào coi là write/không-đọc. (Việc reject `/*!` bất kể dialect trong
        sqlguard._normalize đã đóng exploit MySQL exec-comment cụ thể ở cả hai call-site; đây
        là lớp phòng thủ bổ sung cho các khác biệt dialect khác.)
        """
        if isinstance(driver, str) and driver:
            return classify_sql(sql, driver)
        dec = Decision("deny", "không xác định được dialect SQL", "SQL_NO_DIALECT")
        for d in _SQL_DIALECTS:
            dec = classify_sql(sql, d)
            if dec.verdict != "allow":
                return dec
        return dec
