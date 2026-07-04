"""Memory Review Gate (spec P0-P1 §3.8, P1.5.2).

Agent KHÔNG ghi thẳng MEMORY.md. Đề xuất → staging memory/pending/; người vận hành
duyệt merge. Khác chủ đích so với 3 repo tham chiếu (đều cho agent tự ghi).
"""

from __future__ import annotations

import uuid
from pathlib import Path


class MemoryReviewGate:
    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root
        self._pending = workspace_root / "memory" / "pending"

    def propose(self, content: str) -> str:
        """Agent đề xuất một mẩu memory → ghi vào staging. Trả id đề xuất."""
        self._pending.mkdir(parents=True, exist_ok=True)
        pid = uuid.uuid4().hex[:8]
        (self._pending / f"{pid}.md").write_text(content, encoding="utf-8")
        return pid

    def list_pending(self) -> list[tuple[str, str]]:
        if not self._pending.exists():
            return []
        return sorted(
            (p.stem, p.read_text(encoding="utf-8")) for p in self._pending.glob("*.md")
        )

    def approve(self, pid: str) -> bool:
        """Người vận hành duyệt → append vào MEMORY.md, xóa khỏi staging."""
        src = self._pending / f"{pid}.md"
        if not src.exists():
            return False
        memory = self._root / "MEMORY.md"
        prev = memory.read_text(encoding="utf-8") if memory.exists() else ""
        memory.write_text(prev + "\n\n" + src.read_text(encoding="utf-8"), encoding="utf-8")
        src.unlink()
        return True

    def reject(self, pid: str) -> bool:
        src = self._pending / f"{pid}.md"
        if src.exists():
            src.unlink()
            return True
        return False
