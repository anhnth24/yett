"""Test list_dir / grep — scoped, chống đọc lọt ngoài project."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yett.errors import UserFacingError
from yett.tools.builtin.codenav import GrepTool, ListDirTool, SearchTool
from yett.tools.projects import ProjectScope


class _Ctx:
    session_key = "t"


def _scope(root: Path) -> ProjectScope:
    return ProjectScope(root, {"proj": root})


def _mkproj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.go").write_text("package main\nfunc handleOrder() {}\n", encoding="utf-8")
    (root / "src" / "util.go").write_text("package main\nfunc noop() {}\n", encoding="utf-8")
    (root / "README.md").write_text("# proj\nhandleOrder docs\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("handleOrder in deps", encoding="utf-8")
    return root


def test_list_dir_lists_and_skips_noise(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    tool = ListDirTool(_scope(root))
    res = asyncio.run(tool.run({"path": str(root), "depth": 2}, _Ctx()))
    assert res.ok
    assert "main.go" in res.content and "README.md" in res.content
    assert "node_modules/ (bỏ qua)" in res.content  # nhiễu bị bỏ


def test_list_dir_out_of_scope_denied(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    tool = ListDirTool(_scope(root))
    try:
        asyncio.run(tool.run({"path": str(tmp_path)}, _Ctx()))  # cha của scope
        assert False, "phải từ chối"
    except UserFacingError:
        pass


def test_grep_finds_matches_with_location(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    tool = GrepTool(_scope(root))
    res = asyncio.run(tool.run({"pattern": "handleOrder", "path": str(root)}, _Ctx()))
    assert res.ok
    assert "main.go:2" in res.content
    assert "README.md:2" in res.content
    assert "node_modules" not in res.content  # dir nhiễu không bị grep


def test_grep_glob_filter(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    tool = GrepTool(_scope(root))
    res = asyncio.run(tool.run({"pattern": "handleOrder", "path": str(root), "glob": "*.go"}, _Ctx()))
    assert "main.go" in res.content and "README.md" not in res.content


def test_grep_out_of_scope_denied(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    (tmp_path / "secret.txt").write_text("handleOrder", encoding="utf-8")
    tool = GrepTool(_scope(root))
    try:
        asyncio.run(tool.run({"pattern": "handleOrder", "path": str(tmp_path)}, _Ctx()))
        assert False, "phải từ chối"
    except UserFacingError:
        pass


def test_grep_bad_regex_rejected(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    tool = GrepTool(_scope(root))
    try:
        tool.validate({"pattern": "([unclosed"})
        assert False
    except UserFacingError:
        pass


def test_search_batches_grep_and_read(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    tool = SearchTool(_scope(root))
    args = {
        "grep": [{"pattern": "handleOrder", "path": str(root), "glob": "*.go"}],
        "read": [str(root / "README.md")],
        "list": [str(root / "src")],
    }
    tool.validate(args)
    res = asyncio.run(tool.run(args, _Ctx()))
    assert res.ok
    assert "### grep 'handleOrder'" in res.content and "main.go" in res.content
    assert "### read" in res.content and "handleOrder docs" in res.content
    assert "### list" in res.content and "util.go" in res.content


def test_search_needs_at_least_one_op(tmp_path: Path) -> None:
    tool = SearchTool(_scope(_mkproj(tmp_path)))
    try:
        tool.validate({})
        assert False
    except UserFacingError:
        pass


def test_search_op_cap(tmp_path: Path) -> None:
    tool = SearchTool(_scope(_mkproj(tmp_path)))
    try:
        tool.validate({"read": [f"f{i}" for i in range(11)]})
        assert False
    except UserFacingError:
        pass


def test_search_out_of_scope_reported_not_leaked(tmp_path: Path) -> None:
    root = _mkproj(tmp_path)
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    tool = SearchTool(_scope(root))
    args = {"read": [str(tmp_path / "secret.txt")]}
    res = asyncio.run(tool.run(args, _Ctx()))
    assert res.is_error
    assert "TOPSECRET" not in res.content  # ngoài scope → không đọc được nội dung


# --- ReDoS guard: regex/scan chậm không được treo cả turn ---
def test_grep_regex_timeout_denied_not_hung(tmp_path: Path, monkeypatch) -> None:
    """[ReDoS guard] Không cần regex thảm hại thật (chậm/không ổn định) — giả lập việc
    scan chạy quá lâu bằng cách hạ timeout xuống rất thấp + làm `_search_all` chậm hơn
    ngưỡng đó; phải trả lỗi agent-đọc-được thay vì treo/raise ra ngoài."""
    import time

    from yett.tools.builtin import codenav

    root = _mkproj(tmp_path)
    tool = GrepTool(_scope(root))
    monkeypatch.setattr(codenav, "_REGEX_TIMEOUT_SEC", 0.05)

    def _slow_search_all(self, roots, rx, glob, cap):
        time.sleep(0.3)
        return []

    monkeypatch.setattr(GrepTool, "_search_all", _slow_search_all)
    res = asyncio.run(tool.run({"pattern": "handleOrder", "path": str(root)}, _Ctx()))
    assert res.is_error
    assert "DENIED" in res.content and "0.05" in res.content


# --- `_section` không để 1 sub-op lỗi crash cả batch ---
async def test_search_section_survives_unexpected_exception(tmp_path: Path, monkeypatch) -> None:
    root = _mkproj(tmp_path)
    tool = SearchTool(_scope(root))

    async def _boom(args, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tool._grep, "run", _boom)
    args = {"grep": [{"pattern": "handleOrder"}], "read": [str(root / "README.md")]}
    tool.validate(args)
    res = await tool.run(args, _Ctx())
    assert res.is_error  # section grep lỗi
    assert "[lỗi không mong đợi]" in res.content and "kaboom" in res.content
    assert "handleOrder docs" in res.content  # section read khác vẫn chạy bình thường
