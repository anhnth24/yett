"""BasicGate (spec P0-P1 §3.5) — Policy Gate v0.1 hard-coded.

Thứ tự: hardline deny-list → allowlist → approval tier → DEFAULT DENY.
Ở P3, PolicyEngine thay chỗ này qua CÙNG interface evaluate() — call-site không đổi.
"""

from __future__ import annotations

from yett.config.models import SecurityCfg
from yett.security import allowlist, cmdguard, denylist
from yett.security.gate import Decision, SessionCtx


class BasicGate:
    def __init__(self, security: SecurityCfg, hosts=None) -> None:
        self._sec = security
        self._hosts = hosts  # HostRegistry | None — để phân lớp lệnh SSH theo host profile

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

        # SSH: phân lớp qua cmdguard theo host profile (readonly allow / deploy approval /
        # xóa file hardline deny / còn lại default deny). Host lạ → deny.
        if tool == "ssh_exec" and self._hosts is not None:
            return self._gate_ssh(args)
        if tool == "log_read" and self._hosts is not None:
            return self._gate_log_read(args)

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

    def _gate_ssh(self, args: dict) -> Decision:
        host_name = str(args.get("host", ""))
        if not self._hosts.has(host_name):
            return Decision("deny", f"host '{host_name}' chưa đăng ký — không cho SSH đại", "SSH_UNKNOWN_HOST")
        host = self._hosts.resolve(host_name)
        return cmdguard.gate_ssh(str(args.get("cmd", "")), deploy_script=host.deploy_script, tier=host.tier)

    def _gate_log_read(self, args: dict) -> Decision:
        host_name = str(args.get("host", ""))
        if not self._hosts.has(host_name):
            return Decision("deny", f"host '{host_name}' chưa đăng ký", "SSH_UNKNOWN_HOST")
        # log_read chỉ tail read-only trong log_paths (tool tự kiểm path) → cho phép.
        return Decision("allow", "log_read read-only", "LOG_READ")
