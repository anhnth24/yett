"""Hardline deny-list (spec P0-P1 §3.5). Hard-coded ở v0.1; không config nào override được.

Đây là tầng cứng nhất: dù allowlist/policy cố cho phép, những thứ này luôn bị chặn.
"""

from __future__ import annotations

import re

from yett.security.gate import Decision

# Ranh giới KẾT THÚC từ khoá nguy hiểm: không phải word-char/hyphen theo sau.
# `\b` đơn thuần coi "-" là điểm ngắt từ, nên "format-patch" (git subcommand hợp lệ, không
# phải "format" + đối số) vẫn khớp `format\b` → false-deny. Yêu cầu KHÔNG có "-" nối tiếp thì
# giữ nguyên khả năng bắt "rm"/"del" khi đứng sau wrapper (vd "sudo rm -rf /", "time rm x") —
# ranh giới ĐẦU lệnh cố tình giữ nguyên (kể cả khoảng trắng) vì đây là tầng hardline cuối cùng
# của BasicGate cho tool "exec" (không có bước bóc wrapper như cmdguard).
_KEYWORD_END = r"(?![\w-])"

# Lệnh exec hardline POSIX (Linux/macOS) — áp cho tool exec local + nền cho SSH
_DENY_EXEC_POSIX = re.compile(
    r"""
    (^|[\s;&|`$(])            # ranh giới lệnh
    (rm|rmdir|unlink|shred|mkfs|dd)"""
    + _KEYWORD_END
    + r"""   # binary phá hoại
    | :\(\)\s*\{              # fork bomb
    | \bchmod\s+-R\s+777\s+/  # nới quyền toàn hệ thống
    """,
    re.VERBOSE,
)

# Lệnh exec hardline WINDOWS (cmd/PowerShell) — cho chạy native Windows
_DENY_EXEC_WIN = re.compile(
    r"""
    (?ix)
    (^|[\s;&|(])
    ( del | erase                       # xóa file
    | rd | rmdir                        # xóa thư mục (rd /s xoá đệ quy)
    | remove-item | ri | rm | del       # PowerShell alias
    | format                            # format ổ — KHÔNG khớp khi nối "-" (vd git format-patch)
    | clear-content                     # xoá nội dung file
    )"""
    + _KEYWORD_END
    + r"""
    | \bfsutil\b
    | \bdiskpart\b
    """,
    re.VERBOSE,
)

# Đường dẫn nhạy cảm không được đọc/ghi (POSIX + Windows)
_DENY_PATH = re.compile(
    r"(/etc/shadow|/etc/sudoers|\.ssh/id_|\.aws/credentials|/root/\.|"
    r"\\Windows\\System32\\config|\\SAM\b|%SystemRoot%)",
    re.IGNORECASE,
)


def check_exec(cmd: str) -> Decision | None:
    """Trả Decision(deny) nếu lệnh chạm hardline; None nếu không.
    Kiểm cả POSIX lẫn Windows (không phụ thuộc OS đang chạy — an toàn khi exec qua SSH/container)."""
    if _DENY_EXEC_POSIX.search(cmd) or _DENY_EXEC_WIN.search(cmd):
        return Decision("deny", "hardline: lệnh phá hoại/xóa file bị cấm tuyệt đối", "DENY_EXEC")
    return None


def check_path(path: str) -> Decision | None:
    if _DENY_PATH.search(path):
        return Decision("deny", "hardline: truy cập đường dẫn nhạy cảm bị cấm", "DENY_PATH")
    return None
