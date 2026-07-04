"""Test memory: workspace loader + review gate + FTS5 store (RG1-6, RG1-8, RG2-7)."""

from __future__ import annotations

from pathlib import Path

from yett.memory.review_gate import MemoryReviewGate
from yett.memory.store import MemoryStore
from yett.memory.workspace import WorkspaceMemory


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
