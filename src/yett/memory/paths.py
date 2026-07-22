"""Đường dẫn memory workspace dùng chung (tránh hard-code rải rác).

`MEMORY_FILENAME` là tên file curated dài hạn; staging nằm ở `memory/pending/`.
"""

from __future__ import annotations

from pathlib import Path

MEMORY_FILENAME = "MEMORY.md"
PENDING_PARTS = ("memory", "pending")
MEMORY_LOCK_FILENAME = ".yett-memory-review.lock"
MEMORY_AUDIT_FILENAME = "review-audit.jsonl"
MEMORY_CONTROL_FILENAMES = {
    MEMORY_LOCK_FILENAME,
    MEMORY_AUDIT_FILENAME,
}


def is_memory_file(path: Path) -> bool:
    return path.name == MEMORY_FILENAME


def is_pending_staging_path(path: Path) -> bool:
    """True nếu path nằm dưới .../memory/pending/ (kể cả chưa tồn tại trên đĩa)."""
    parts_l = [p.lower() for p in path.parts]
    for i in range(len(parts_l) - 1):
        if parts_l[i] == PENDING_PARTS[0] and parts_l[i + 1] == PENDING_PARTS[1]:
            return True
    return False


def is_memory_control_file(path: Path) -> bool:
    """Review lock/audit metadata are never agent-writable."""
    names = {name.casefold() for name in MEMORY_CONTROL_FILENAMES}
    return path.name.casefold() in names
