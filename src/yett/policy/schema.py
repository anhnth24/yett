"""Policy schema (spec P3 §2.1). Rule = điều kiện → hiệu ứng. Hardline luôn > rule config."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


class Match(BaseModel):
    tool: str | None = None  # "ssh_exec" | "db_query" | "*"
    args: dict[str, str] = Field(default_factory=dict)  # regex trên args
    session: str | None = None
    data_class: str | None = None

    def matches(self, tool: str, args: dict, ctx) -> bool:
        if self.tool not in (None, "*", tool):
            return False
        for k, pat in self.args.items():
            if not re.search(pat, str(args.get(k, ""))):
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
