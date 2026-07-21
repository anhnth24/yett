"""RG1-8: không module nào ngoài memory.review_gate ghi thẳng MEMORY.md.

Kiểm source-level: ngoài `memory/review_gate.py`, không file nào trong src/yett
vừa nhắc `MEMORY.md` vừa gọi API ghi (`write_text` / `open(..., "w"|"a"|"x")`).
`workspace.py` chỉ đọc; tool ghi đi qua staging (`memory_propose`).
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "yett"
_ALLOWED_WRITE = SRC / "memory" / "review_gate.py"


def _mentions_memory_md(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "MEMORY.md" in node.value:
                return True
    return False


def _has_write_call(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # path.write_text(...) / open(..., "w")
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"write_text", "write_bytes"}:
            return True
        if isinstance(func, ast.Name) and func.id == "open":
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if any(m in arg.value for m in ("w", "a", "x")):
                        return True
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
                    if isinstance(mode, str) and any(m in mode for m in ("w", "a", "x")):
                        return True
    return False


def test_only_review_gate_writes_memory_md() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.resolve() == _ALLOWED_WRITE.resolve():
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        if _mentions_memory_md(tree) and _has_write_call(tree):
            offenders.append(str(path.relative_to(SRC.parent)))
    assert offenders == [], (
        "RG1-8: chỉ memory/review_gate.py được ghi MEMORY.md; "
        f"phát hiện vừa nhắc MEMORY.md vừa có write API: {offenders}"
    )
