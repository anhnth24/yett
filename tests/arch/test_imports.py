"""Test kiến trúc: các bất biến import (spec P0-P1 §1, AG-3).

Không phụ thuộc import-linter CLI — kiểm trực tiếp source để chạy được ngay ở skeleton.
Khi cài import-linter đầy đủ, CI chạy thêm `lint-imports` cho bộ contract đầy đủ.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "yett"


def _imports_of(pkg: str) -> set[str]:
    found: set[str] = set()
    pkg_dir = SRC / pkg
    for path in pkg_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


def test_core_does_not_import_sandbox() -> None:
    # Bất biến #1: core loop không gọi thẳng sandbox (phải qua tools.wiring).
    assert not any(m.startswith("yett.sandbox") for m in _imports_of("core"))


def test_core_and_tools_do_not_import_provider_adapters() -> None:
    # core/tools chỉ biết provider.base, không biết adapter cụ thể.
    for pkg in ("core", "tools"):
        imps = _imports_of(pkg)
        assert "yett.provider.anthropic" not in imps
        assert "yett.provider.openai_compat" not in imps
