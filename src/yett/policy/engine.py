"""Policy Engine (spec P3 §2.2). Thay BasicGate qua CÙNG interface evaluate() → Decision.

Thứ tự: immutable hardline → rule config theo priority → DEFAULT DENY.
Call-site (wiring) không đổi — chỉ hoán backend. Toàn bộ test BasicGate phải pass.
"""

from __future__ import annotations

from typing import Literal

from yett.policy.immutable import ImmutableCore
from yett.policy.schema import PolicyFile
from yett.security.gate import Decision, SessionCtx

_Verdict = Literal["allow", "deny", "need_approval"]


class PolicyEngine:
    def __init__(self, policy: PolicyFile, immutable: ImmutableCore) -> None:
        self._rules = sorted(policy.rules, key=lambda r: -r.priority)
        self._immutable = immutable

    def evaluate(self, tool: str, args: dict, ctx: SessionCtx) -> Decision:
        # 1) immutable hardline — không rule config nào đảo được
        if hit := self._immutable.check(tool, args):
            return hit
        # 2) rule config theo priority
        for r in self._rules:
            if r.match.matches(tool, args, ctx):
                mapping: dict[str, _Verdict] = {
                    "allow": "allow", "deny": "deny", "approve": "need_approval", "redact": "allow"
                }
                return Decision(mapping[r.effect], f"policy rule {r.id}", r.id)
        # 3) DEFAULT DENY
        return Decision("deny", "no policy rule matched — default deny", "DEFAULT_DENY")
