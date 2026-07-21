"""Workspace memory loader (spec P0-P1 §3.8). File-based, token budget, deterministic.

Nạp các file workspace (AGENTS.md, SOUL.md, TOOLS.md, MEMORY.md, daily notes) theo
thứ tự ưu tiên; truncate khi vượt budget. Triết lý: model chỉ nhớ thứ ghi xuống đĩa.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yett.core.context import estimate_tokens
from yett.memory.paths import MEMORY_FILENAME

# Thứ tự ưu tiên nạp (cao xuống thấp) — khi vượt budget, cắt từ cuối danh sách.
_LOAD_ORDER = ["SOUL.md", "AGENTS.md", "TOOLS.md", MEMORY_FILENAME]


@dataclass
class WorkspaceMemory:
    root: Path

    def _read(self, name: str) -> str | None:
        p = self.root / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return None

    def daily_notes(self, days: list[str]) -> list[tuple[str, str]]:
        """days = ['2026-07-04', ...]; trả (tên, nội dung) tồn tại."""
        out = []
        for d in days:
            p = self.root / "memory" / f"{d}.md"
            if p.exists():
                out.append((f"memory/{d}.md", p.read_text(encoding="utf-8", errors="replace")))
        return out

    def build_system_prompt(self, base: str, *, token_budget: int, days: list[str] | None = None) -> str:
        """Ghép system prompt từ base + file workspace, tôn trọng token budget.

        Deterministic: luôn nạp theo _LOAD_ORDER rồi daily notes; cắt phần vượt budget
        từ cuối (ưu tiên giữ SOUL/AGENTS)."""
        parts: list[tuple[str, str]] = [("base", base)]
        for name in _LOAD_ORDER:
            content = self._read(name)
            if content:
                parts.append((name, content))
        parts.extend(self.daily_notes(days or []))

        out: list[str] = []
        used = 0
        for name, content in parts:
            block = f"\n\n### {name}\n{content}" if name != "base" else content
            t = estimate_tokens(block)
            if used + t > token_budget and name != "base":
                out.append(f"\n\n### {name}\n[truncated — vượt token budget]")
                break
            out.append(block)
            used += t
        return "".join(out)
