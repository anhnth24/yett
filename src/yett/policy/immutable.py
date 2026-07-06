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

_IMMUTABLE_WRITE = Decision(
    "deny", "HARDLINE: không được sửa policy/identity (immutable core)", "IMMUTABLE_WRITE",
)


class ImmutableCore:
    def __init__(self, protected_paths: list[Path] | None = None) -> None:
        self._protected = [p.resolve() for p in (protected_paths or [])]
        # [P1-8/RT-4/H1] Tên (basename) các vùng protected — dùng quét substring trong chuỗi
        # lệnh exec/ssh_exec (không biết cwd thật local/remote, và command có thể lách qua
        # redirect/interpreter/shell-lồng nên không bóc tách operand đáng tin). Xem
        # `_exec_hits_protected`.
        self._protected_names = {p.name for p in self._protected}

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
        # ghi vào vùng immutable (policy/identity) — write_file và exec/ssh_exec là 2 case
        # KHÁC HẲN nhau (RT-4): write_file có 1 path THẬT trong args['path']; exec/ssh_exec
        # có CẢ MỘT CÂU LỆNH trong args['cmd'] (vd `sed -i policy.yaml`), không phải 1 path.
        if tool == "write_file":
            if self._write_file_hits_protected(args.get("path")):
                return _IMMUTABLE_WRITE
        if tool in ("exec", "ssh_exec"):
            if self._exec_hits_protected(str(args.get("cmd", ""))):
                return _IMMUTABLE_WRITE
        return None

    def _write_file_hits_protected(self, raw_path: object) -> bool:
        """[RT-4] write_file: args['path'] là path THẬT → canonicalize (resolve()) rồi so
        is_relative_to với từng protected dir/file. Đây là case dễ (khác exec, xem
        `_exec_hits_protected`) vì không có shell syntax cần bóc tách."""
        if not raw_path or not self._protected:
            return False
        try:
            target = Path(str(raw_path)).resolve()
        except (OSError, ValueError):
            return True  # path không hợp lệ/không resolve được → fail-closed, coi là chạm
        for p in self._protected:
            if target == p or target.is_relative_to(p):
                return True
        return False

    def _exec_hits_protected(self, cmd: str) -> bool:
        """[P1-8/RT-4/H1] exec/ssh_exec: target là CẢ CÂU LỆNH, không phải 1 path. KHÔNG thể tin
        cậy bóc tách operand vì có quá nhiều dạng lách: redirect dính (`>f`, `2>>f`, `>|f`
        clobber), interpreter inline (`python -c "open('cfg','w')"`), shell lồng (`sh -c "..."`),
        quoting/`$()`. Vì vậy fail-closed đơn giản & bao trùm: nếu TÊN của bất kỳ vùng protected
        xuất hiện ở BẤT KỲ đâu trong chuỗi lệnh → DENY. Đây là hàng rào cứng defense-in-depth;
        containment THẬT đến từ Docker (rootfs read-only + file config nằm NGOÀI mount ghi được).
        Đánh đổi đã biết: có thể over-deny lệnh CHỈ đọc config (vd `cat harness.yaml`) — chấp
        nhận, vì lớp này là hardline chứ không phải bộ lọc chính xác (write_file mới dùng
        resolve()+is_relative_to chính xác, xem `_write_file_hits_protected`)."""
        if not cmd or not self._protected_names:
            return False
        return any(name in cmd for name in self._protected_names)

    @staticmethod
    def _classify_db_query(sql: str, driver: object) -> Decision:
        """[RT-5/RT-18] Phân loại SQL cho db_query ở lớp immutable-core.

        TRƯỚC: gọi `classify_sql(sql)` mặc định dialect='postgres' — sai lệch với driver thật
        (vd MySQL) khiến vài payload chỉ nguy hiểm ở dialect thật lọt qua vì postgres phân loại
        khác đi. `db_query.py` tự nó gọi `classify_sql(sql, prof.driver)` đúng driver đã resolve
        từ profile; nhưng Gate/PolicyEngine chạy TRƯỚC khi tool.run() resolve profile nên args ở
        đây chỉ có 'sql' thô từ model, chưa có driver thật.

        Vì vậy: nếu caller (hiện tại `tools/wiring.py` thread từ DbQueryTool đã đăng ký khi có
        thể — xem Phase 6 handoff — hoặc tương lai) truyền sẵn `driver` trong args → dùng đúng
        dialect đó. Nếu không có → KHÔNG mặc định một dialect duy nhất — fail-closed bằng cách
        phân loại qua TẤT CẢ dialect được hỗ trợ và deny nếu bất kỳ dialect nào coi là
        write/không-đọc. (Việc reject `/*!` bất kể dialect trong sqlguard._normalize đã đóng
        exploit MySQL exec-comment cụ thể ở cả hai call-site; đây là lớp phòng thủ bổ sung cho
        các khác biệt dialect khác.)
        """
        if isinstance(driver, str) and driver:
            return classify_sql(sql, driver)
        dec = Decision("deny", "không xác định được dialect SQL", "SQL_NO_DIALECT")
        for d in _SQL_DIALECTS:
            dec = classify_sql(sql, d)
            if dec.verdict != "allow":
                return dec
        return dec
