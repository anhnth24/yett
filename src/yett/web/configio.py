"""Đọc/ghi config qua web UI (xem + sửa). Validate bằng pydantic trước khi lưu (fail-closed)."""

from __future__ import annotations

from pathlib import Path

import yaml

from yett.config.models import HarnessCfg


def read_config_text(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


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
