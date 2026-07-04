"""Test fail-closed của Policy Gate (spec P0-P1 §3.3, tiêu chí T-RG1-2a).

Đây là một trong các test sống/chết: Gate raise exception → caller nhận DENY,
không ném lỗi lên loop. Guard AG-1 bảo đảm test này không bị xoá/skip.
"""

from __future__ import annotations

from yett.security.gate import Decision, safe_evaluate


class _BoomGate:
    def evaluate(self, tool: str, args: dict, ctx: object) -> Decision:
        raise RuntimeError("gate bị lỗi nội bộ")


class _Ctx:
    session_key = "test"


def test_gate_exception_results_in_deny() -> None:
    dec = safe_evaluate(_BoomGate(), "exec", {"cmd": "ls"}, _Ctx())
    assert dec.verdict == "deny"
    assert dec.rule_id == "GATE_ERROR"


def test_normal_decision_passes_through() -> None:
    class _AllowGate:
        def evaluate(self, tool: str, args: dict, ctx: object) -> Decision:
            return Decision("allow", "ok", "TEST_ALLOW")

    dec = safe_evaluate(_AllowGate(), "exec", {"cmd": "ls"}, _Ctx())
    assert dec.verdict == "allow"
