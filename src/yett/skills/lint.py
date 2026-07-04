"""Lint description skill (spec P2 P2.1.3). Description tốt quyết định trigger đúng."""

from __future__ import annotations

from yett.errors import UserFacingError

_MIN_DESC = 20
_VAGUE = {"skill", "tool", "helper", "does things", "misc", "utility", "test"}


def lint_description(name: str, description: str) -> None:
    """Raise UserFacingError nếu description quá ngắn/mơ hồ (chặn cài)."""
    d = description.strip()
    if len(d) < _MIN_DESC:
        raise UserFacingError(
            f"skill '{name}': description quá ngắn ({len(d)}<{_MIN_DESC}). "
            f"Mô tả rõ KHI NÀO dùng skill này để trigger đúng."
        )
    if d.lower() in _VAGUE:
        raise UserFacingError(f"skill '{name}': description mơ hồ '{d}' — nói rõ khi nào dùng")
