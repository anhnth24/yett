"""Allowlist per-deployment (spec P0-P1 §3.5). Match tool + regex trên args."""

from __future__ import annotations

import posixpath
import re

from yett.config.models import ToolRule
from yett.security.gate import Decision

# Arg trông giống path (host chạy exec/read_file/write_file HOẶC path WSL2/container mà
# harness quảng cáo hỗ trợ — xem config/harness.example.yaml phần "projects") — chuẩn hóa
# trước khi so khớp (xem `_normalize_arg_value`). Khác `log_read` (remote POSIX qua SSH, xem
# `ssh_exec.py`), nhưng CÙNG lý do tránh `Path.resolve()`/`os.path.normpath` gắn cứng quy tắc
# separator của OS đang chạy: harness chạy Windows nhưng path project trong config lại là
# POSIX-style (`/mnt/d/...` WSL2) — `os.path.normpath` trên Windows sẽ đổi hết '/' thành '\\'
# và làm pattern POSIX-style trong rule không còn khớp được nữa dù không có traversal.
_PATH_LIKE_KEYS = {"path", "cwd"}


def match_allowlist(tool: str, args: dict, rules: list[ToolRule]) -> Decision | None:
    """Trả Decision (allow/need_approval) nếu có rule khớp; None nếu không rule nào khớp."""
    for rule in rules:
        if rule.tool not in (tool, "*"):
            continue
        if _args_match(args, rule.arg_patterns):
            if rule.effect == "allow":
                return Decision("allow", f"allowlist: {rule.tool}", f"ALLOW_{rule.tool}")
            return Decision("need_approval", f"allowlist: {rule.tool}", f"ALLOW_{rule.tool}")
    return None


def _normalize_arg_value(key: str, val: str) -> str:
    """[anchor] Chuẩn hóa lexical cho arg trông giống path TRƯỚC khi so khớp — anchor
    (fullmatch) một mình không đủ: `path: "/workspace/../etc/passwd"` vẫn LITERALLY bắt đầu
    và có thể fullmatch một pattern tưởng đã giới hạn đúng thư mục. Gộp '..'/'.'  thuần theo
    chuỗi (`posixpath.normpath`, sau khi đổi '\\' → '/') — KHÔNG dùng `os.path.normpath` hay
    `Path.resolve()` (phụ thuộc separator/OS đang chạy, xem comment `_PATH_LIKE_KEYS`)."""
    if key not in _PATH_LIKE_KEYS:
        return val
    return posixpath.normpath(val.replace("\\", "/"))


def _args_match(args: dict, patterns: dict[str, str]) -> bool:
    """[P2/allowlist-anchor] `fullmatch` thay vì `search` — trước đây pattern không tự anchor
    (vd `{"cmd": "ls"}`) khớp NHẦM cả khi "ls" chỉ là substring giữa lệnh khác (`ls; rm`,
    `xls`), vì `re.search` chấp nhận khớp một phần bất kỳ đâu trong chuỗi. Rule muốn cho phép
    nhiều biến thể phải tự viết pattern đủ (vd `^echo .*$`), không còn ngầm định "search"."""
    for key, pat in patterns.items():
        val = _normalize_arg_value(key, str(args.get(key, "")))
        if not re.fullmatch(pat, val):
            return False
    return True
