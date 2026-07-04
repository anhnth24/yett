"""Policy Gate (spec P0-P1 §3.3).

Bất biến sống/chết: mọi tool call xuyên qua Gate TRƯỚC khi thực thi.
Thứ tự đánh giá: hardline deny-list → allowlist → approval tier → DEFAULT DENY.
Fail-closed: bất kỳ exception nào trong evaluate() → caller nhận deny (dùng safe_evaluate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

log = logging.getLogger("yett.gate")


@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "deny", "need_approval"]
    reason: str  # agent đọc được khi deny
    rule_id: str | None = None


class SessionCtx(Protocol):
    """Ngữ cảnh session mà Gate cần để quyết định (đầy đủ hoá khi làm core)."""

    session_key: str


class PolicyGate(Protocol):
    def evaluate(self, tool: str, args: dict, ctx: SessionCtx) -> Decision: ...


def safe_evaluate(gate: PolicyGate, tool: str, args: dict, ctx: SessionCtx) -> Decision:
    """Bọc evaluate() để đảm bảo fail-closed (spec §3.3).

    Đây là ĐIỂM DUY NHẤT core loop được phép gọi Gate — không gọi evaluate() trực tiếp.
    Test T-RG1-2a bảo đảm: Gate raise → trả deny, không ném lỗi lên loop.
    """
    try:
        return gate.evaluate(tool, args, ctx)
    except Exception as e:  # noqa: BLE001 — cố ý bắt mọi lỗi để fail-closed
        log.error("gate_failure tool=%s err=%s", tool, e)
        return Decision("deny", "policy gate error — denied by fail-closed", "GATE_ERROR")
