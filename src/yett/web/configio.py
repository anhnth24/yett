"""Đọc/ghi config qua web UI (xem + sửa). Validate bằng pydantic trước khi lưu (fail-closed)."""

from __future__ import annotations

from pathlib import Path

import yaml

from yett.config.models import HarnessCfg


def read_config_text(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_config_text_redacted(path: str | Path) -> str:
    """Đọc config để hiển thị qua web UI, đã redact secret (dùng chung filter Phase 5
    — `yett.security.filters.redact`, KHÔNG tự viết lại pattern, tránh lặp lỗ hổng cũ
    như miss format `sk-cp-`).

    LƯU Ý (trade-off có chủ đích): endpoint lưu (`write_config_text`) KHÔNG merge lại
    giá trị gốc — nếu người dùng bấm Lưu mà không sửa dòng đã bị che, giá trị secret
    thật trên đĩa sẽ bị ghi đè bằng chuỗi `[REDACTED]` literal. Khuyến nghị (UI đã nhắc
    ở tab Cấu hình): giữ secret trong `secrets/` hoặc biến môi trường, không sửa secret
    trực tiếp qua tab này.
    """
    from yett.security.filters import redact

    return redact(read_config_text(path))


def validate_config_text(text: str) -> str | None:
    """Trả None nếu hợp lệ; ngược lại trả thông báo lỗi (không lưu nếu lỗi)."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return f"YAML lỗi cú pháp: {e}"
    try:
        HarnessCfg.model_validate(raw)
    except Exception as e:  # noqa: BLE001 — trả lỗi cho UI
        return f"Config không hợp lệ: {e}"
    return None


def write_config_text(path: str | Path, text: str) -> str | None:
    """Validate rồi ghi. Trả None nếu ok, hoặc thông báo lỗi (không ghi)."""
    err = validate_config_text(text)
    if err is not None:
        return err
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return None
