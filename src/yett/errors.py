"""Phân loại lỗi (spec P0-P1 §0).

Hai loại lỗi tách bạch để xử lý khác nhau:
- UserFacingError: agent đọc được và tự sửa được (vd sai schema tool). Trả về cho agent.
- SystemError: bug hệ thống. Với Gate/secret, mọi lỗi → fail-closed (deny), không lộ ra loop.
"""

from __future__ import annotations


class YettError(Exception):
    """Gốc cho mọi lỗi của yett."""


class UserFacingError(YettError):
    """Lỗi agent đọc được, sửa được — nội dung message là hướng dẫn sửa."""


class SystemError(YettError):
    """Lỗi hệ thống (bug/hạ tầng). Không dành cho agent đọc."""


class DenyError(YettError):
    """Policy Gate từ chối một hành động. Mang theo lý do agent đọc được."""

    def __init__(self, reason: str, rule_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_id = rule_id


class SecretNotFound(SystemError):
    """Secret store không tìm thấy tên secret — fail-closed."""
