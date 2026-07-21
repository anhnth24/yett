"""Test memory: workspace loader + review gate + FTS5 store (RG1-6, RG1-8, RG2-7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from yett.config.models import SecurityCfg, ToolRule
from yett.memory.review_gate import MemoryReviewGate
from yett.memory.store import MemoryStore
from yett.memory.workspace import WorkspaceMemory
from yett.security.basic_gate import BasicGate
from yett.tools.builtin.files import WriteFileTool
from yett.tools.builtin.memory import MemoryProposeTool
from yett.tools.projects import ProjectScope
from yett.tools.registry import Registry
from yett.tools.wiring import execute_tool


def test_workspace_deterministic_and_budget(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("Tôi là trợ lý.", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Quy tắc vận hành.", encoding="utf-8")
    wm = WorkspaceMemory(tmp_path)
    p1 = wm.build_system_prompt("BASE", token_budget=1000)
    p2 = wm.build_system_prompt("BASE", token_budget=1000)
    assert p1 == p2  # deterministic (RG1-6)
    assert "Tôi là trợ lý." in p1 and "Quy tắc vận hành." in p1


def test_workspace_truncates_over_budget(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("x" * 100, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("y" * 10000, encoding="utf-8")
    wm = WorkspaceMemory(tmp_path)
    out = wm.build_system_prompt("BASE", token_budget=50)
    assert "truncated" in out


def test_review_gate_flow(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("Khách X thích format báo cáo ngắn.")
    assert len(gate.list_pending()) == 1
    # MEMORY.md chưa có nội dung (RG1-8: agent không ghi thẳng)
    assert not (tmp_path / "MEMORY.md").exists()
    assert gate.approve(pid)
    assert "Khách X" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert len(gate.list_pending()) == 0


def test_review_gate_reject(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("đề xuất sai")
    assert gate.reject(pid)
    assert not (tmp_path / "MEMORY.md").exists()


def test_review_gate_reject_does_not_touch_existing_memory(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("giữ nguyên", encoding="utf-8")
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("đề xuất sẽ bị loại")
    assert gate.reject(pid)
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "giữ nguyên"
    assert len(gate.list_pending()) == 0


def test_review_gate_path_isolation_rejects_traversal(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    assert gate.approve("../escape") is False
    assert gate.approve("abcd1234/../x") is False
    assert gate.approve("../../etc/passwd") is False
    assert gate.reject("not-hex!!") is False
    assert gate.list_pending() == []


def test_review_gate_project_roots_do_not_share_pending(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    ga = MemoryReviewGate(ws_a)
    gb = MemoryReviewGate(ws_b)
    pid = ga.propose("chỉ thuộc workspace A")
    assert len(ga.list_pending()) == 1
    assert gb.list_pending() == []
    assert gb.approve(pid) is False
    assert (ws_b / "MEMORY.md").exists() is False
    assert ga.approve(pid)
    assert "workspace A" in (ws_a / "MEMORY.md").read_text(encoding="utf-8")


def test_review_gate_empty_content_fail_closed(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    with pytest.raises(ValueError, match="trống"):
        gate.propose("   ")


def test_memory_store_search_vietnamese(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "sessions.db")
    store.add_message("s1", "user", "kiểm tra đơn hàng số 123 bị lỗi", ts=1.0)
    store.add_message("s1", "assistant", "đơn hàng đã được xử lý", ts=2.0)
    store.add_message("s2", "user", "deploy lên UAT thành công", ts=3.0)
    # tìm cụm tiếng Việt có dấu (trigram)
    hits = store.search("đơn hàng")
    assert any("123" in h["content"] for h in hits)
    # kết quả là message gốc
    assert all("content" in h and "role" in h for h in hits)
    deploy = store.search("UAT")
    assert any("deploy" in h["content"] for h in deploy)
    store.close()


class _Ctx:
    def __init__(self, session_key: str = "main", *, is_subagent: bool = False) -> None:
        self.session_key = session_key
        self.is_subagent = is_subagent


def _reg_with_memory(ws: Path) -> tuple[Registry, MemoryReviewGate]:
    gate = MemoryReviewGate(ws)
    reg = Registry()
    reg.register(MemoryProposeTool(gate))
    reg.register(WriteFileTool(ProjectScope(ws, {})))
    return reg, gate


async def test_memory_propose_via_gate_registry_filters_no_direct_write(tmp_path: Path) -> None:
    """Đường chạy thật: Gate → Registry → Filters; propose chỉ tạo staging."""
    ws = tmp_path / "ws"
    ws.mkdir()
    reg, gate = _reg_with_memory(ws)
    rules = [ToolRule(tool="memory_propose", effect="allow")]
    g = BasicGate(SecurityCfg(allowlist=rules))
    res = await execute_tool(
        "memory_propose",
        {"content": "Khách X thích báo cáo ngắn."},
        _Ctx("main"),
        gate=g,
        registry=reg,
    )
    assert not res.is_error
    assert "id=" in res.content
    assert not (ws / "MEMORY.md").exists()
    assert len(gate.list_pending()) == 1
    pid = gate.list_pending()[0][0]
    assert gate.approve(pid)
    assert "Khách X" in (ws / "MEMORY.md").read_text(encoding="utf-8")


async def test_memory_propose_fail_closed_without_allowlist(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    reg, gate = _reg_with_memory(ws)
    g = BasicGate(SecurityCfg(allowlist=[]))  # default deny
    res = await execute_tool(
        "memory_propose", {"content": "không được ghi"}, _Ctx("main"), gate=g, registry=reg,
    )
    assert res.is_error and "DENIED" in res.content
    assert gate.list_pending() == []
    assert not (ws / "MEMORY.md").exists()


async def test_memory_propose_session_isolation(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    reg, gate = _reg_with_memory(ws)
    g = BasicGate(SecurityCfg(allowlist=[ToolRule(tool="memory_propose", effect="allow")]))
    for sk in ("tg:99", "other", "main:sub:x"):
        res = await execute_tool(
            "memory_propose", {"content": "x"}, _Ctx(sk), gate=g, registry=reg,
        )
        assert res.is_error and "DENIED" in res.content
    sub = await execute_tool(
        "memory_propose", {"content": "x"}, _Ctx("main", is_subagent=True), gate=g, registry=reg,
    )
    assert sub.is_error and "DENIED" in sub.content
    assert gate.list_pending() == []


async def test_write_file_cannot_direct_write_memory_md(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    reg, _gate = _reg_with_memory(ws)
    g = BasicGate(SecurityCfg(allowlist=[ToolRule(tool="write_file", effect="allow")]))
    res = await execute_tool(
        "write_file",
        {"path": str(ws / "MEMORY.md"), "content": "lách review gate"},
        _Ctx("main"),
        gate=g,
        registry=reg,
    )
    assert res.is_error
    assert "memory_propose" in res.content.lower() or "MEMORY.md" in res.content
    assert not (ws / "MEMORY.md").exists()


async def test_write_file_cannot_write_pending_staging(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "memory" / "pending").mkdir(parents=True)
    reg, gate = _reg_with_memory(ws)
    g = BasicGate(SecurityCfg(allowlist=[ToolRule(tool="write_file", effect="allow")]))
    res = await execute_tool(
        "write_file",
        {"path": str(ws / "memory" / "pending" / "deadbeef.md"), "content": "bypass"},
        _Ctx("main"),
        gate=g,
        registry=reg,
    )
    assert res.is_error
    assert gate.list_pending() == []


async def test_memory_propose_redacts_secrets_before_staging(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    reg, gate = _reg_with_memory(ws)
    g = BasicGate(SecurityCfg(allowlist=[ToolRule(tool="memory_propose", effect="allow")]))
    secret = "sk-ant-" + ("A" * 24)
    res = await execute_tool(
        "memory_propose",
        {"content": f"key của anh là {secret}"},
        _Ctx("main"),
        gate=g,
        registry=reg,
    )
    assert not res.is_error
    pending = gate.list_pending()
    assert len(pending) == 1
    assert secret not in pending[0][1]
    assert "[REDACTED]" in pending[0][1]
