"""BasicGate (spec P0-P1 §3.5) — Policy Gate v0.1 hard-coded.

Thứ tự: hardline deny-list → allowlist → approval tier → DEFAULT DENY.
Ở P3, PolicyEngine thay chỗ này qua CÙNG interface evaluate() — call-site không đổi.
"""

from __future__ import annotations

from yett.config.models import SecurityCfg
from yett.security import allowlist, denylist
from yett.security.gate import Decision, SessionCtx


class BasicGate:
    def __init__(self, security: SecurityCfg) -> None:
        self._sec = security

    def evaluate(self, tool: str, args: dict, ctx: SessionCtx) -> Decision:
        # 1) hardline deny-list — không gì override được
        if tool in ("exec", "ssh_exec"):
            cmd = str(args.get("cmd", ""))
            if hit := denylist.check_exec(cmd):
                return hit
        if tool in ("read_file", "write_file"):
            path = str(args.get("path", ""))
            if hit := denylist.check_path(path):
                return hit
        # 2) allowlist per-deployment
        if dec := allowlist.match_allowlist(tool, args, self._sec.allowlist):
            return dec
        # 3) DEFAULT DENY (fail-closed)
        return Decision(
            "deny",
            f"tool '{tool}' không nằm trong allowlist — mặc định từ chối. "
            f"Thêm rule vào config nếu cần.",
            "DEFAULT_DENY",
        )
