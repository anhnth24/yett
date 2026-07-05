"""Immutable core (spec P3 §2.3). Hardline không rule config nào đảo được + identity read-only.

check() trả deny cho: lệnh phá hoại (denylist), SSH xóa file, DB write, và mọi hành vi
chạm ghi vào vùng immutable (policy file, identity file). Subagent kế thừa, không nới.
"""

from __future__ import annotations

import shlex
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


def _basename(token: str) -> str:
    """Basename thuần chuỗi, chấp cả '/' (POSIX/remote) và '\\\\' (Windows local) làm
    separator — token đến từ shlex-parse một command line, không phải path đã biết OS nào."""
    return token.replace("\\", "/").rsplit("/", 1)[-1]


class ImmutableCore:
    def __init__(self, protected_paths: list[Path] | None = None) -> None:
        self._protected = [p.resolve() for p in (protected_paths or [])]
        # [P1-8/RT-4] fail-closed fallback cho exec/ssh_exec: không biết cwd thật (local hay
        # remote qua SSH) nên không thể canonicalize path tương đối như write_file — so khớp
        # basename thay vì bỏ qua hoàn toàn.
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
        """[P1-8/RT-4] exec/ssh_exec: target là CẢ CÂU LỆNH (vd `sed -i policy.yaml`), KHÔNG
        phải 1 path — `Path(cmd).resolve()` vô nghĩa và `is_relative_to` không bắt được (còn
        tệ hơn substring cho path tuyệt đối, vì nó không bắt được gì cả). Sửa: shlex-parse
        lệnh, lấy từng operand (bỏ tên lệnh ở vị trí 0 và các cờ bắt đầu bằng '-'), so khớp
        BASENAME với tên file protected — fail-closed đơn giản (không cần biết cwd thật, có
        thể là local hay remote qua SSH) như kiến trúc phase đã chấp nhận. Không parse được
        (shlex lỗi, vd quote không khớp) → fail-closed, coi là chạm.

        Giới hạn đã biết (không phải bug, ghi nhận rõ): lệnh lồng nested shell qua `sh -c "..."`
        không được đệ quy bóc tách — token `-c "..."` là 1 chuỗi duy nhất với shlex ở tầng
        ngoài, basename của cả chuỗi đó hiếm khi trùng tên file protected. Ngoài phạm vi P1-8.
        """
        if not cmd or not self._protected_names:
            return False
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return True  # không parse được lệnh → fail-closed
        for i, tok in enumerate(tokens):
            if i == 0 or tok.startswith("-"):
                continue  # tên lệnh + cờ không phải operand path
            if _basename(tok) in self._protected_names:
                return True
        return False

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
