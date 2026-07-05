"""ssh_exec + log_read tools (spec P2 §2.3).

Gate (cmdguard) chạy ở tầng wiring/gate; ở đây chạy thật qua SSH backend injectable.
SSH key lấy từ secret store tại điểm dùng, KHÔNG vào context/span.
"""

from __future__ import annotations

from typing import Protocol

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult
from yett.tools.remote.hostprofile import HostProfile, HostRegistry


class SshBackend(Protocol):
    async def run(self, host: HostProfile, cmd: str, *, key: str) -> tuple[int, str, str]:
        """Trả (exit_code, stdout, stderr). key = nội dung keyfile (không log)."""
        ...


class SshExecTool:
    """Chạy lệnh trên server qua SSH (chỉ host đã đăng ký; hardline cấm xóa file)."""

    name = "ssh_exec"
    schema = {
        "type": "object",
        "properties": {"host": {"type": "string"}, "cmd": {"type": "string"}},
        "required": ["host", "cmd"],
    }

    def __init__(self, hosts: HostRegistry, backend: SshBackend, secrets, vpn=None) -> None:
        self._hosts = hosts
        self._backend = backend
        self._secrets = secrets
        self._vpn = vpn

    def validate(self, args: dict) -> None:
        if not args.get("host") or not args.get("cmd"):
            raise UserFacingError("cần 'host' và 'cmd'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        host = self._hosts.resolve(args["host"])  # host lạ → UserFacingError
        if host.vpn_required and self._vpn is not None:
            await self._vpn.ensure(host.vpn_required)
        key = self._resolve_key(host)
        code, out, err = await self._backend.run(host, args["cmd"], key=key)
        body = f"exit={code}\n{out}"
        if err:
            body += f"\n[stderr]\n{err}"
        return ToolResult(ok=code == 0, content=body, is_error=code != 0)

    def _resolve_key(self, host: HostProfile) -> str:
        if host.auth.startswith("keyfile:"):
            return str(self._secrets.get(host.auth.split(":", 1)[1]))
        return ""


class LogReadTool:
    """Đọc log trên server (chỉ đường dẫn trong log_paths của host — read-only)."""

    name = "log_read"
    schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string"}, "path": {"type": "string"}, "lines": {"type": "integer"}
        },
        "required": ["host", "path"],
    }

    def __init__(self, hosts: HostRegistry, backend: SshBackend, secrets, vpn=None) -> None:
        self._hosts = hosts
        self._backend = backend
        self._secrets = secrets
        self._vpn = vpn

    def validate(self, args: dict) -> None:
        if not args.get("host") or not args.get("path"):
            raise UserFacingError("cần 'host' và 'path'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        host = self._hosts.resolve(args["host"])
        path = args["path"]
        if not _path_allowed(path, host.log_paths):
            return ToolResult.error(
                f"[DENIED] '{path}' không nằm trong log_paths khai báo của host"
            )
        if host.vpn_required and self._vpn is not None:
            await self._vpn.ensure(host.vpn_required)
        key = self._secrets.get(host.auth.split(":", 1)[1]) if host.auth.startswith("keyfile:") else ""
        lines = int(args.get("lines", 200))
        code, out, err = await self._backend.run(host, f"tail -n {lines} {_q(path)}", key=key)
        return ToolResult(ok=code == 0, content=out or err, is_error=code != 0)


def _path_allowed(path: str, allow: list[str]) -> bool:
    """[P1-9/RT-13] Containment thật cho path log trên server REMOTE (luôn POSIX — server SSH
    không phải Windows) — chuẩn hóa LEXICAL thuần chuỗi (`posixpath.normpath`), KHÔNG chạm
    filesystem: path này ở xa, và controller có thể chạy Windows (harness quảng cáo đa nền,
    CI có leg Windows) nên `Path.resolve()` local sẽ diễn giải theo quy tắc Windows — sai, và
    nguy hiểm hơn là có thể đụng nhầm filesystem LOCAL thay vì chỉ tính toán chuỗi.

    Sau chuẩn hóa: path phải TUYỆT ĐỐI, cùng thư mục cha với đúng 1 pattern trong `allow`, và
    (nếu pattern có wildcard) khớp glob CHỈ trên phần TÊN FILE — không cho `*` nuốt qua '/'.
    Trước đây dùng `fnmatch.fnmatch(path, pattern)` trên CẢ ĐƯỜNG DẪN: '*' của fnmatch khớp
    cả '/' lẫn '..' nên `/var/log/app/../../etc/passwd.log` lọt qua pattern
    `/var/log/app/*.log` (kết thúc bằng .log, "chứa" phần giữa bất kỳ) — traversal thật ra
    vùng ngoài log_paths khai báo dù nhìn qua tưởng bị chặn."""
    import fnmatch
    import posixpath
    from pathlib import PurePosixPath

    norm = PurePosixPath(posixpath.normpath(path))
    if not norm.is_absolute() or ".." in norm.parts:
        return False  # tương đối hoặc còn '..' sau chuẩn hóa (vượt gốc) → fail-closed
    for pat in allow:
        norm_pat = PurePosixPath(posixpath.normpath(pat))
        if norm == norm_pat:
            return True
        if norm.parent == norm_pat.parent and fnmatch.fnmatchcase(norm.name, norm_pat.name):
            return True
    return False


def _q(path: str) -> str:
    import shlex

    return shlex.quote(path)
