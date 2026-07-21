"""Memory Review Gate (spec P0-P1 §3.8, P1.5.2).

Agent KHÔNG ghi thẳng MEMORY.md. Đề xuất → staging memory/pending/; người vận hành
duyệt merge. Khác chủ đích so với 3 repo tham chiếu (đều cho agent tự ghi).

ĐÂY LÀ MODULE DUY NHẤT được phép mở workspace/MEMORY.md ở mode ghi (RG1-8).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from yett.memory.paths import MEMORY_FILENAME, PENDING_PARTS

# Id đề xuất do gate cấp — chỉ hex ngắn; chặn path traversal / tên lạ.
_PID_RE = re.compile(r"^[a-f0-9]{8}$")
_MAX_CONTENT_CHARS = 16_384


class MemoryReviewGate:
    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root.resolve()
        self._pending = self._root.joinpath(*PENDING_PARTS)

    @property
    def workspace_root(self) -> Path:
        return self._root

    @property
    def pending_dir(self) -> Path:
        return self._pending

    def propose(self, content: str) -> str:
        """Agent đề xuất một mẩu memory → ghi vào staging. Trả id đề xuất.

        Không bao giờ chạm MEMORY.md. Nội dung rỗng / quá dài → ValueError (caller
        đổi thành lỗi agent-đọc-được).
        """
        text = (content or "").strip()
        if not text:
            raise ValueError("nội dung đề xuất memory trống")
        if len(text) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"nội dung đề xuất vượt {_MAX_CONTENT_CHARS} ký tự — rút gọn rồi thử lại"
            )
        self._pending.mkdir(parents=True, exist_ok=True)
        pid = uuid.uuid4().hex[:8]
        dest = self._pending_path(pid)
        if dest is None:  # không xảy ra với uuid hex[:8]; giữ fail-closed cho type/checker
            raise ValueError("không tạo được id đề xuất hợp lệ")
        # Ghi staging thôi — KHÔNG đụng MEMORY.md.
        dest.write_text(text, encoding="utf-8")
        return pid

    def list_pending(self) -> list[tuple[str, str]]:
        if not self._pending.exists():
            return []
        out: list[tuple[str, str]] = []
        for p in sorted(self._pending.glob("*.md")):
            if not _PID_RE.match(p.stem):
                continue
            out.append((p.stem, p.read_text(encoding="utf-8")))
        return out

    def approve(self, pid: str) -> bool:
        """Người vận hành duyệt → append vào MEMORY.md, xóa khỏi staging."""
        src = self._pending_path(pid)
        if src is None or not src.exists():
            return False
        memory = self._root / MEMORY_FILENAME
        prev = memory.read_text(encoding="utf-8") if memory.exists() else ""
        chunk = src.read_text(encoding="utf-8")
        memory.write_text(prev + ("\n\n" if prev else "") + chunk, encoding="utf-8")
        src.unlink()
        return True

    def reject(self, pid: str) -> bool:
        src = self._pending_path(pid)
        if src is None or not src.exists():
            return False
        src.unlink()
        return True

    def _pending_path(self, pid: str) -> Path | None:
        """Resolve path staging an toàn — pid lạ / traversal → None (fail-closed)."""
        if not isinstance(pid, str) or not _PID_RE.match(pid):
            return None
        dest = (self._pending / f"{pid}.md").resolve()
        try:
            if not dest.is_relative_to(self._pending.resolve()):
                return None
        except (OSError, ValueError):
            return None
        return dest
