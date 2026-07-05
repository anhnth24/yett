"""Test list_dir / grep — scoped, chống đọc lọt ngoài project."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yett.errors import UserFacingError
from yett.tools.builtin.codenav import GrepTool, ListDirTool
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
