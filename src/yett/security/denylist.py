"""Hardline deny-list (spec P0-P1 §3.5). Hard-coded ở v0.1; không config nào override được.

Đây là tầng cứng nhất: dù allowlist/policy cố cho phép, những thứ này luôn bị chặn.
"""

from __future__ import annotations

import re

from yett.security.gate import Decision

# Lệnh exec hardline (áp cho tool exec local + là nền cho SSH ở P2)
_DENY_EXEC = re.compile(
    r"""
    (^|[\s;&|`$(])            # ranh giới lệnh
    (rm|rmdir|unlink|shred|mkfs|dd)\b   # binary phá hoại
    | :\(\)\s*\{              # fork bomb
    | \bchmod\s+-R\s+777\s+/  # nới quyền toàn hệ thống
    """,
    re.VERBOSE,
)

# Đường dẫn nhạy cảm không được đọc/ghi
_DENY_PATH = re.compile(r"(/etc/shadow|/etc/sudoers|\.ssh/id_|\.aws/credentials|/root/\.)")


def check_exec(cmd: str) -> Decision | None:
    """Trả Decision(deny) nếu lệnh chạm hardline; None nếu không."""
    if _DENY_EXEC.search(cmd):
        return Decision("deny", f"hardline: lệnh phá hoại bị cấm tuyệt đối", "DENY_EXEC")
    return None


def check_path(path: str) -> Decision | None:
    if _DENY_PATH.search(path):
        return Decision("deny", "hardline: truy cập đường dẫn nhạy cảm bị cấm", "DENY_PATH")
    return None
