"""Config loader (spec P0-P1 §2). Đọc YAML → validate pydantic; lỗi → raise (fail-closed)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from yett.config.models import HarnessCfg, PriceRow
from yett.errors import SystemError


def load_config(path: str | Path) -> HarnessCfg:
    p = Path(path)
    if not p.exists():
        raise SystemError(f"config không tồn tại: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        return HarnessCfg.model_validate(raw)
    except ValidationError as e:  # config hỏng → từ chối khởi động
        raise SystemError(f"config không hợp lệ: {e}") from e


def load_pricing(path: str | Path) -> dict[str, dict[str, PriceRow]]:
    """pricing.yaml → {provider: {model: PriceRow}}. Thiếu file → bảng rỗng (cost=0)."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, PriceRow]] = {}
    for provider, models in raw.items():
        out[provider] = {m: PriceRow.model_validate(row) for m, row in models.items()}
    return out
