"""Cancel token (spec P0-P1 §3.3 core, P1.2.6). Hủy sạch ở ranh giới stage."""

from __future__ import annotations


class Cancelled(Exception):
    """Turn bị hủy tại một ranh giới stage."""


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        """Gọi ở đầu mỗi stage/vòng lặp. Raise Cancelled nếu đã hủy."""
        if self._cancelled:
            raise Cancelled()
