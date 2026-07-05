"""Policy schema (spec P3 §2.1). Rule = điều kiện → hiệu ứng. Hardline luôn > rule config."""

from __future__ import annotations

import posixpath
import re
from typing import Literal

from pydantic import BaseModel, Field

# Arg trông giống path — chuẩn hóa trước khi so khớp (cùng lý do với
# `security/allowlist.py._normalize_arg_value`: harness chạy Windows nhưng path project trong
# config lại là POSIX-style/WSL2, nên KHÔNG dùng `os.path.normpath`/`Path.resolve()` — đổi
# separator theo OS đang chạy sẽ làm pattern POSIX-style không còn khớp được. Giữ 2 bản riêng
# (allowlist.py + đây) vì Match ở policy layer không nên import ngược security.allowlist chỉ
# để dùng 1 hàm 1 dòng).
_PATH_LIKE_KEYS = {"path", "cwd"}


class Match(BaseModel):
    tool: str | None = None  # "ssh_exec" | "db_query" | "*"
    args: dict[str, str] = Field(default_factory=dict)  # regex trên args
    session: str | None = None
    data_class: str | None = None

    def matches(self, tool: str, args: dict, ctx) -> bool:
        if self.tool not in (None, "*", tool):
            return False
        for k, pat in self.args.items():
            val = str(args.get(k, ""))
            if k in _PATH_LIKE_KEYS:
                val = posixpath.normpath(val.replace("\\", "/"))
            # [P2/allowlist-anchor] `fullmatch` thay `search` — pattern không tự anchor (vd
            # "ls") trước đây khớp NHẦM khi chỉ là substring (`ls; rm`). Rule cần khớp nhiều
            # biến thể phải tự viết pattern đủ (vd `^echo .*$`).
            if not re.fullmatch(pat, val):
                return False
        if self.session is not None and getattr(ctx, "session_key", None) != self.session:
            return False
        return True


class Rule(BaseModel):
    id: str
    match: Match
    effect: Literal["allow", "deny", "approve", "redact"]
    priority: int = 0


class PolicyFile(BaseModel):
    rules: list[Rule] = Field(default_factory=list)
