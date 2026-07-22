"""Test memory: workspace loader + review gate + FTS5 store (RG1-6, RG1-8, RG2-7)."""

from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from yett.config.models import SecurityCfg, ToolRule
from yett.memory.review_gate import MemoryReviewError, MemoryReviewGate, is_valid_proposal_id
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


def _approve_in_process(item: tuple[str, str]) -> bool:
    workspace, pid = item
    return MemoryReviewGate(Path(workspace)).approve(pid)


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


async def test_write_file_blocks_lexical_memory_symlink_before_resolution(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "ordinary.txt"
    target.write_text("unchanged", encoding="utf-8")
    (ws / "MEMORY.md").symlink_to(target)
    reg, _gate = _reg_with_memory(ws)
    g = BasicGate(SecurityCfg(allowlist=[ToolRule(tool="write_file", effect="allow")]))
    res = await execute_tool(
        "write_file",
        {"path": str(ws / "MEMORY.md"), "content": "bypass"},
        _Ctx("main"),
        gate=g,
        registry=reg,
    )
    assert res.is_error
    assert target.read_text(encoding="utf-8") == "unchanged"


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


def test_review_gate_redacts_before_disk_even_when_called_directly(tmp_path: Path) -> None:
    secret = "sk-ant-" + ("Z" * 24)
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose(f"credential={secret}")
    raw = (gate.pending_dir / f"{pid}.md").read_text(encoding="utf-8")
    assert secret not in raw
    assert "[REDACTED]" in raw


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_review_gate_rejects_pending_content_tampered_after_proposal(
    tmp_path: Path, action: str
) -> None:
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("audited original")
    staged = gate.pending_dir / f"{pid}.md"
    staged.write_text("tampered replacement", encoding="utf-8")

    with pytest.raises(MemoryReviewError, match="khớp audit"):
        getattr(gate, action)(pid)

    assert staged.read_text(encoding="utf-8") == "tampered replacement"
    assert not (tmp_path / "MEMORY.md").exists()


def test_review_gate_rejects_pending_directory_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "pending").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemoryReviewError, match="an toàn"):
        MemoryReviewGate(tmp_path).propose("must not escape")
    assert list(outside.iterdir()) == []


def test_review_gate_rejects_staged_file_symlink(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    gate.ensure_storage()
    victim = tmp_path / "victim"
    victim.write_text("outside secret", encoding="utf-8")
    pid = "a" * 32
    (gate.pending_dir / f"{pid}.md").symlink_to(victim)
    with pytest.raises(MemoryReviewError, match="không an toàn"):
        gate.approve(pid)
    assert victim.read_text(encoding="utf-8") == "outside secret"
    assert not (tmp_path / "MEMORY.md").exists()


def test_review_gate_rejects_memory_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_text("unchanged", encoding="utf-8")
    (tmp_path / "MEMORY.md").symlink_to(target)
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("approved text")
    with pytest.raises(MemoryReviewError, match="không an toàn"):
        gate.approve(pid)
    assert target.read_text(encoding="utf-8") == "unchanged"
    assert gate.list_pending()[0][0] == pid


def test_review_gate_concurrent_distinct_approvals_do_not_lose_updates(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    proposals = [(gate.propose(f"item-{i}"), f"item-{i}") for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: MemoryReviewGate(tmp_path).approve(item[0]), proposals))
    assert results == [True] * len(proposals)
    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    lines = memory.splitlines()
    for pid, content in proposals:
        assert lines.count(content) == 1
        assert memory.count(f"yett-memory-proposal:{pid}") == 1
    assert gate.list_pending() == []


def test_review_gate_concurrent_same_approval_is_exactly_once(tmp_path: Path) -> None:
    pid = MemoryReviewGate(tmp_path).propose("only once")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: MemoryReviewGate(tmp_path).approve(pid), range(2)))
    assert sorted(results) == [False, True]
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8").count("only once") == 1


def test_review_gate_cross_process_approval_is_exactly_once(tmp_path: Path) -> None:
    pid = MemoryReviewGate(tmp_path).propose("cross-process")
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_approve_in_process, [(str(tmp_path), pid)] * 2))
    assert sorted(results) == [False, True]
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8").count("cross-process") == 1


def test_review_gate_audit_chain_records_lifecycle_and_detects_tamper(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path, clock=lambda: 7.0)
    approved = gate.propose("approved")
    rejected = gate.propose("rejected")
    assert gate.approve(approved)
    assert gate.reject(rejected)
    audit_path = tmp_path / "memory" / "review-audit.jsonl"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [row["action"] for row in rows] == [
        "proposed", "proposed", "approved", "rejected",
    ]
    assert all(row["ts"] == 7.0 and "content_sha256" in row for row in rows)
    audit_path.write_text(audit_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(MemoryReviewError, match="hash-chain"):
        gate.propose("must fail closed")


def test_review_gate_rolls_back_approval_when_audit_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("retryable")
    original = gate._append_audit

    def fail_approved(*args, **kwargs):
        if kwargs["action"] == "approved":
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(gate, "_append_audit", fail_approved)
    with pytest.raises(MemoryReviewError, match="thất bại an toàn"):
        gate.approve(pid)
    assert not (tmp_path / "MEMORY.md").exists()
    assert gate.list_pending()[0][0] == pid


def test_review_gate_recovers_when_staging_unlink_fails_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yett.memory.review_gate as review_mod

    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("durable approval")
    staged_name = f"{pid}.md"
    real_unlink = review_mod.os.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        path_name = getattr(path, "name", path)
        if path_name == staged_name and not failed:
            failed = True
            raise OSError("simulated crash before staging cleanup")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(review_mod.os, "unlink", fail_once)
    with pytest.raises(MemoryReviewError, match="thất bại an toàn"):
        gate.approve(pid)

    # MEMORY + audit committed, staging remains.  Retry consumes it without a duplicate merge
    # or duplicate terminal audit record.
    assert (gate.pending_dir / staged_name).exists()
    assert gate.approve(pid)
    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("durable approval") == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "memory" / "review-audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["action"] for row in rows] == ["proposed", "approved"]


def test_review_gate_detects_memory_in_place_write_toctou(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "MEMORY.md").write_text("base", encoding="utf-8")
    gate = MemoryReviewGate(tmp_path)
    pid = gate.propose("new entry")
    real_read = MemoryReviewGate._read_regular_snapshot_at
    calls = 0

    def racing_read(directory: int | Path, name: str):
        nonlocal calls
        if name == "MEMORY.md":
            calls += 1
            if calls == 2:
                if isinstance(directory, Path):
                    fd = os.open(directory / name, os.O_WRONLY | os.O_TRUNC)
                else:
                    fd = os.open(name, os.O_WRONLY | os.O_TRUNC, dir_fd=directory)
                os.write(fd, b"raced")
                os.close(fd)
        return real_read(directory, name)

    monkeypatch.setattr(
        MemoryReviewGate, "_read_regular_snapshot_at", staticmethod(racing_read)
    )
    with pytest.raises(MemoryReviewError, match="TOCTOU"):
        gate.approve(pid)
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "raced"
    assert gate.list_pending()[0][0] == pid


@pytest.mark.parametrize(
    "pid",
    ["../escape", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 32 + "/x", 123],
)
def test_proposal_id_validation_is_exact(pid: object) -> None:
    assert is_valid_proposal_id(pid) is False
