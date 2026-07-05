"""File secret store (spec P0-P1 §3.2). Lưu secret vào secrets/<name>, quyền owner-only.

Cho wizard setup: key nằm trên đĩa trong thư mục secrets/ (gitignored, owner-only) —
KHÔNG nằm trong config, KHÔNG vào context/span/log. Đơn giản hơn quản lý env cho user local.

[P1-13] POSIX: chmod 600/700 (owner-only rwx). Windows: os.chmod không map sang DACL
NTFS (no-op) — dùng `icacls` (subprocess, không thêm dep pywin32) để đặt DACL owner-only
thật. LƯU Ý: `/inheritance:r` chỉ bỏ ACE KẾ THỪA; ACE explicit có sẵn (vd `Everyone:(F)`
trên thư mục tmp mở) vẫn sống sót — nên sau khi grant owner phải liệt kê DACL thật và
`/remove` mọi principal khác owner, rồi verify lại. Nếu không đặt được ACL (icacls lỗi/
thiếu, ACE lạ không gỡ được) → CẢNH BÁO TO qua log + stderr; secret vẫn được ghi (không
chặn wizard) — không bao giờ nuốt lỗi im lặng (`except OSError: pass` cũ đã bỏ).
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

from yett.errors import SecretNotFound

logger = logging.getLogger(__name__)


def _warn_permission_failure(path: Path, detail: str) -> None:
    """Cảnh báo TO khi không khoá được quyền file secret — không bao giờ nuốt lỗi im lặng."""
    msg = (
        f"[yett] CẢNH BÁO BẢO MẬT: không đặt được quyền owner-only cho '{path}': {detail}. "
        "File secret có thể bị đọc bởi user khác trên máy này — kiểm tra thủ công."
    )
    logger.warning(msg)
    print(msg, file=sys.stderr)


def _current_user() -> tuple[str, str] | None:
    """Lấy (tên account `domain\\user`, SID) của user hiện tại qua `whoami /user`
    (tránh nhập nhằng domain\\user khi tên máy trùng tên user — icacls có thể resolve
    sai sang máy thay vì user; tên dùng để so khớp ACE trong output icacls)."""
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = result.stdout.strip()
    if not line:
        return None
    fields = [f.strip().strip('"') for f in line.split(",")]
    if len(fields) < 2 or not fields[0] or not fields[-1].startswith("S-1-"):
        return None
    return fields[0], fields[-1]


def _icacls_principals(path: Path, icacls_output: str) -> list[str]:
    """Parse tên principal từ output `icacls <path>` — mỗi ACE một dòng dạng
    `TÊN:(quyền)`; dòng đầu có prefix path, dòng tổng kết không chứa `:(`."""
    text = icacls_output.replace(str(path), "", 1)
    principals: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if ":(" in line:
            principals.append(line.split(":(", 1)[0])
    return principals


def _restrict_windows_acl(path: Path) -> None:
    """Đặt DACL owner-only bằng icacls, 4 bước fail-loud:
    1) `/inheritance:r` (bỏ ACE kế thừa) + 2) `/grant:r *SID:F` (owner Full Control).
    3) `/inheritance:r` KHÔNG xoá ACE explicit có sẵn (Everyone/BUILTIN\\Users/... trên
       thư mục mở) → liệt kê DACL thật, `/remove` từng principal khác owner (dùng đúng
       tên icacls in ra nên không phụ thuộc locale).
    4) Verify lại: còn ACE nào không phải owner → cảnh báo to (không nuốt).
    Không dùng pywin32 (không thêm dep — theo Risk Assessment)."""
    user = _current_user()
    if user is None:
        _warn_permission_failure(path, "không lấy được SID user hiện tại (whoami /user lỗi)")
        return

    def _icacls(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["icacls", str(path), *args],
            capture_output=True, text=True, check=True, timeout=10,
        )

    account_name, sid = user
    try:
        _icacls("/inheritance:r")
        _icacls("/grant:r", f"*{sid}:F")
        for principal in _icacls_principals(path, _icacls().stdout):
            if principal.lower() != account_name.lower():
                _icacls("/remove", principal)
        leftover = [
            p for p in _icacls_principals(path, _icacls().stdout)
            if p.lower() != account_name.lower()
        ]
        if leftover:
            _warn_permission_failure(
                path, f"ACE còn lại cho principal khác owner (không gỡ được): {leftover}"
            )
    except (OSError, subprocess.SubprocessError) as e:
        stderr = e.stderr if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
        _warn_permission_failure(path, f"icacls thất bại: {stderr}")


def _restrict_permissions(path: Path, *, mode: int) -> None:
    """mode: bit POSIX mong muốn (vd 0o600, 0o700) — chỉ áp dụng trên POSIX.
    Trên Windows dùng ACL (icacls) thay vì mode bit."""
    if sys.platform == "win32":
        _restrict_windows_acl(path)
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        _warn_permission_failure(path, str(e))


class FileSecretStore:
    def __init__(self, root: str | Path = "secrets") -> None:
        self._root = Path(root)

    def set(self, name: str, value: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self._root, mode=stat.S_IRWXU)  # 700
        p = self._root / name
        p.write_text(value, encoding="utf-8")
        _restrict_permissions(p, mode=stat.S_IRUSR | stat.S_IWUSR)  # 600

    def get(self, name: str) -> str:
        p = self._root / name
        if not p.exists():
            raise SecretNotFound(f"secret '{name}' không tồn tại tại {p}")
        return p.read_text(encoding="utf-8").strip()

    def has(self, name: str) -> bool:
        return (self._root / name).exists()
