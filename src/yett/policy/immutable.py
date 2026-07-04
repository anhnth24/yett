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
            dec = classify_sql(str(args.get("sql", "")))
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
