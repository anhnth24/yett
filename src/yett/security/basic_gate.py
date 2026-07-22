"""BasicGate (spec P0-P1 §3.5) — Policy Gate v0.1 hard-coded.

Thứ tự: hardline deny-list → allowlist → approval tier → DEFAULT DENY.
Ở P3, PolicyEngine thay chỗ này qua CÙNG interface evaluate() — call-site không đổi.
"""

from __future__ import annotations

from yett.config.models import SecurityCfg
from yett.security import allowlist, cmdguard, denylist
from yett.security.gate import Decision, SessionCtx


class BasicGate:
    def __init__(self, security: SecurityCfg, hosts=None, vpn_profiles: set[str] | None = None) -> None:
        self._sec = security
        self._hosts = hosts  # HostRegistry | None — để phân lớp lệnh SSH theo host profile
        # Tên profile VPN đã khai trong config — tool vpn chỉ allow khi profile ∈ tập này.
        self._vpn_profiles = vpn_profiles

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
        if tool == "vpn" and self._vpn_profiles is not None:
            return self._gate_vpn(args)

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

    def _gate_vpn(self, args: dict) -> Decision:
        action = str(args.get("action", ""))
        if action not in ("connect", "disconnect", "status"):
            return Decision("deny", "vpn action phải là connect|disconnect|status", "VPN_BAD_ACTION")
        profile = str(args.get("profile", ""))
        allowed = self._vpn_profiles or set()
        if not profile or profile not in allowed:
            return Decision(
                "deny",
                f"vpn profile '{profile}' không nằm trong allowlist config",
                "VPN_UNKNOWN_PROFILE",
            )
        # Không cho model truyền thêm key (flag/argv). Chỉ action + profile.
        extra = set(args) - {"action", "profile"}
        if extra:
            return Decision(
                "deny",
                f"vpn từ chối tham số lạ {sorted(extra)} — chỉ action+profile",
                "VPN_EXTRA_ARGS",
            )
        if action in {"connect", "disconnect"}:
            return Decision(
                "need_approval",
                f"model yêu cầu VPN {action}; operator CLI không đi qua model gate",
                "VPN_LIFECYCLE_APPROVAL",
            )
        return Decision("allow", "vpn profile đã khai báo", "VPN_PROFILE")
