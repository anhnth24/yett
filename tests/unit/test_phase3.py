"""Phase 3: policy engine parity, immutable core, audit chain, subagent no-escalation.

Map gate: RG3-2 (policy giữ interface), RG3-3 (immutable red-team), RG3-4 (audit chain),
RG3-9 (subagent không leo thang quyền).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yett.compliance.audit import AuditLog
from yett.errors import DenyError, UserFacingError
from yett.policy.engine import PolicyEngine
from yett.policy.immutable import ImmutableCore
from yett.policy.schema import Match, PolicyFile, Rule
from yett.security.gate import safe_evaluate
from yett.subagent.definition import load_subagent
from yett.subagent.delegate import DelegateCtx, DelegateTool


class _Ctx:
    session_key = "main"


# --- RG3-2: policy engine cùng interface, default deny + hardline ---
def _engine(rules=None, protected=None) -> PolicyEngine:
    return PolicyEngine(PolicyFile(rules=rules or []), ImmutableCore(protected or []))


def test_policy_default_deny() -> None:
    dec = safe_evaluate(_engine(), "exec", {"cmd": "echo x"}, _Ctx())
    assert dec.verdict == "deny" and dec.rule_id == "DEFAULT_DENY"


def test_policy_rule_allow() -> None:
    rules = [Rule(id="r1", match=Match(tool="exec", args={"cmd": r"^echo"}), effect="allow")]
    dec = safe_evaluate(_engine(rules), "exec", {"cmd": "echo hi"}, _Ctx())
    assert dec.verdict == "allow" and dec.rule_id == "r1"


def test_policy_priority() -> None:
    rules = [
        Rule(id="low", match=Match(tool="*"), effect="allow", priority=0),
        Rule(id="high", match=Match(tool="exec"), effect="deny", priority=10),
    ]
    dec = safe_evaluate(_engine(rules), "exec", {"cmd": "x"}, _Ctx())
    assert dec.rule_id == "high" and dec.verdict == "deny"


# --- RG3-3: immutable core red-team — hardline thắng cả rule allow ---
def test_immutable_beats_allow_rule() -> None:
    # rule cố allow mọi exec, nhưng rm vẫn bị hardline chặn
    rules = [Rule(id="allowall", match=Match(tool="exec", args={"cmd": ".*"}), effect="allow")]
    dec = safe_evaluate(_engine(rules), "exec", {"cmd": "rm -rf /"}, _Ctx())
    assert dec.verdict == "deny"


def test_immutable_ssh_delete_blocked() -> None:
    rules = [Rule(id="allowall", match=Match(tool="ssh_exec", args={"cmd": ".*"}), effect="allow")]
    dec = safe_evaluate(_engine(rules), "ssh_exec", {"cmd": "xargs rm < list"}, _Ctx())
    assert dec.verdict == "deny" and dec.rule_id == "SSH_DELETE_HARDLINE"


def test_immutable_db_write_blocked() -> None:
    rules = [Rule(id="allowall", match=Match(tool="db_query", args={"sql": ".*"}), effect="allow")]
    dec = safe_evaluate(_engine(rules), "db_query", {"sql": "DELETE FROM t"}, _Ctx())
    assert dec.verdict == "deny"


def test_immutable_protects_policy_file(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("rules: []", encoding="utf-8")
    rules = [Rule(id="allowwrite", match=Match(tool="write_file", args={}), effect="allow")]
    eng = _engine(rules, protected=[policy_file])
    dec = safe_evaluate(eng, "write_file", {"path": str(policy_file), "content": "x"}, _Ctx())
    assert dec.verdict == "deny" and dec.rule_id == "IMMUTABLE_WRITE"


# --- RG3-4: audit hash chain ---
def test_audit_chain_intact(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(ts=1.0, actor="agent", kind="gate", detail={"tool": "exec", "verdict": "deny"})
    log.append(ts=2.0, actor="user", kind="approval", detail={"tool": "deploy", "approved": True})
    assert log.verify_chain() is True
    assert len(log.entries()) == 2


def test_audit_tamper_detected(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p)
    log.append(ts=1.0, actor="agent", kind="gate", detail={"x": 1})
    log.append(ts=2.0, actor="agent", kind="gate", detail={"x": 2})
    # sửa một dòng
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"x": 1', '"x": 999')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log.verify_chain() is False  # phát hiện sửa


def test_audit_redacts_secret(tmp_path: Path) -> None:
    from yett.security.filters import redact_attrs

    log = AuditLog(tmp_path / "audit.jsonl", redactor=redact_attrs)
    log.append(ts=1.0, actor="agent", kind="db_query", detail={"note": "postgres://user:supersecret@h/db"})
    assert "supersecret" not in log.entries()[0]["detail"]["note"]


# --- RG3-9: subagent không leo thang quyền ---
def _mk_subagent(d: Path, name: str, toolset: list[str]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    ts = ", ".join(toolset)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: test\ntoolset: [{ts}]\n---\nprompt", encoding="utf-8"
    )


def test_subagent_toolset_subset_enforced(tmp_path: Path) -> None:
    _mk_subagent(tmp_path, "researcher", ["web_search", "read_file"])
    # cha chỉ có read_file → researcher đòi web_search (vượt) → từ chối load
    with pytest.raises(UserFacingError):
        load_subagent(tmp_path, "researcher", parent_toolset={"read_file"})
    # cha đủ quyền → load ok
    sub = load_subagent(tmp_path, "researcher", parent_toolset={"web_search", "read_file", "exec"})
    assert set(sub.toolset) == {"web_search", "read_file"}


async def test_subagent_no_nested_delegate(tmp_path: Path) -> None:
    _mk_subagent(tmp_path, "worker", ["read_file"])

    async def runner(sub, child, task):
        return "done"

    tool = DelegateTool(tmp_path, runner)
    # ctx là subagent → gọi delegate bị cấm (1 cấp)
    sub_ctx = DelegateCtx(session_key="main:sub:x", allowed_tools={"read_file"}, is_subagent=True)
    with pytest.raises(DenyError):
        await tool.run({"agent": "worker", "task": "x"}, sub_ctx)


async def test_policy_engine_is_drop_in_for_wiring(tmp_path: Path) -> None:
    # RG3-2: PolicyEngine dùng được ở wiring y như BasicGate (cùng interface evaluate).
    from yett.tools.builtin.exec import ExecTool
    from yett.sandbox.local import LocalSandbox
    from yett.tools.registry import Registry
    from yett.tools.wiring import execute_tool

    rules = [Rule(id="echo", match=Match(tool="exec", args={"cmd": r"^echo"}), effect="allow")]
    eng = _engine(rules)
    reg = Registry()
    reg.register(ExecTool(LocalSandbox()))
    ok = await execute_tool("exec", {"cmd": "echo hi"}, _Ctx(), gate=eng, registry=reg)
    assert not ok.is_error and "hi" in ok.content
    # hardline vẫn chặn qua PolicyEngine
    bad = await execute_tool("exec", {"cmd": "rm -rf /"}, _Ctx(), gate=eng, registry=reg)
    assert bad.is_error and "DENIED" in bad.content


async def test_subagent_delegate_runs_child(tmp_path: Path) -> None:
    _mk_subagent(tmp_path, "worker", ["read_file"])
    captured = {}

    async def runner(sub, child, task):
        captured["toolset"] = child.allowed_tools
        captured["is_sub"] = child.is_subagent
        return f"kết quả: {task}"

    tool = DelegateTool(tmp_path, runner)
    parent = DelegateCtx(session_key="main", allowed_tools={"read_file", "web_search"}, is_subagent=False)
    res = await tool.run({"agent": "worker", "task": "tìm X"}, parent)
    assert "kết quả: tìm X" in res.content
    assert captured["toolset"] == {"read_file"}  # con bị thu hẹp
    assert captured["is_sub"] is True
