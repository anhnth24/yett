"""Đọc/ghi config qua web UI (xem + sửa). Validate bằng pydantic trước khi lưu (fail-closed)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from yett.config.models import HarnessCfg

# Phải khớp replacement trong yett.security.filters (mọi pattern redact về cùng chuỗi này).
_REDACT_MARKER = "[REDACTED]"
_KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):[ \t]*(.*?)\s*$")

# Field inline chứa secret THẬT (che theo TÊN field, không dựa vào định dạng value — vì
# redactor theo pattern chỉ bắt sk-…, bỏ sót key GLM `id.secret` là provider mặc định).
# Chỉ che `api_key` (inline key); `api_key_secret`/`dsn_secret` là TÊN tham chiếu, không phải value.
_SECRET_FIELDS = frozenset({"api_key"})


def _redact_secret_fields(text: str) -> str:
    """Che value của field secret inline theo tên field. Bỏ qua dòng comment và value rỗng.
    Ghi ra dạng `key: "[REDACTED]"` để `_restore_redacted_secrets` khôi phục khi lưu."""
    out: list[str] = []
    for line in text.splitlines():
        m = _KV_RE.match(line)
        if m and m.group(2) in _SECRET_FIELDS and m.group(3) and not m.group(3).startswith("#"):
            out.append(f'{m.group(1)}{m.group(2)}: "{_REDACT_MARKER}"')
        else:
            out.append(line)
    joined = "\n".join(out)
    return joined + "\n" if text.endswith("\n") else joined


def read_config_text(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_config_text_redacted(path: str | Path) -> str:
    """Đọc config để hiển thị qua web UI, đã redact secret (dùng chung filter Phase 5
    — `yett.security.filters.redact`, KHÔNG tự viết lại pattern, tránh lặp lỗ hổng cũ
    như miss format `sk-cp-`).

    Khi lưu (`write_config_text`) sẽ merge lại giá trị gốc cho dòng còn chứa marker
    `[REDACTED]` (khớp theo thụt-lề + key), nên bấm Lưu mà không sửa dòng bị che sẽ
    KHÔNG ghi đè secret thật. Khuyến nghị vẫn là giữ secret ở `secrets/` hoặc env.
    """
    from yett.security.filters import redact

    # Hai lớp: (1) che theo TÊN field secret inline (bắt mọi định dạng key, kể cả GLM),
    # (2) redactor pattern Phase 5 (bắt sk-…/PEM/DSN password lọt ở chỗ khác).
    return redact(_redact_secret_fields(read_config_text(path)))


def _restore_redacted_secrets(original_text: str, submitted_text: str) -> str:
    """Khôi phục secret bị che trước khi lưu: dòng `key: ...` nào trong bản gửi lên còn
    chứa marker redact thì lấy lại value nguyên văn từ file gốc trên đĩa (khớp theo
    thụt-lề + key). Giữ nguyên comment/format bản gửi lên; chỉ đụng dòng có marker.

    Chỉ khớp value cả-dòng == một cặp `key: value`. Trùng (indent, key) ở nhiều block
    thì lấy dòng gốc đầu tiên — đủ cho ca thực tế (chỉ block provider active không bị
    comment). Người dùng muốn ĐỔI secret vẫn gõ giá trị mới (không còn marker) như thường.
    """
    if _REDACT_MARKER not in submitted_text:
        return submitted_text
    original_vals: dict[tuple[str, str], str] = {}
    for line in original_text.splitlines():
        m = _KV_RE.match(line)
        if m and _REDACT_MARKER not in m.group(3):
            original_vals.setdefault((m.group(1), m.group(2)), m.group(3))
    out: list[str] = []
    for line in submitted_text.splitlines():
        m = _KV_RE.match(line)
        if m and _REDACT_MARKER in m.group(3):
            orig = original_vals.get((m.group(1), m.group(2)))
            if orig is not None:
                out.append(f"{m.group(1)}{m.group(2)}: {orig}")
                continue
        out.append(line)
    joined = "\n".join(out)
    return joined + "\n" if submitted_text.endswith("\n") else joined


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
    """Merge lại secret bị che, validate, rồi ghi. Trả None nếu ok, hoặc thông báo lỗi
    (không ghi). Merge secret TRƯỚC validate để `[REDACTED]` (parse thành YAML list) không
    làm pydantic fail và không ghi đè secret thật trên đĩa."""
    merged = _restore_redacted_secrets(read_config_text(path), text)
    err = validate_config_text(merged)
    if err is not None:
        return err
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(merged, encoding="utf-8")
    except OSError as e:
        # Prod Docker mount config read-only (:ro) → không ghi được. Báo rõ thay vì 500.
        return (f"Không ghi được config ({e.strerror or e}). Nếu chạy Docker, config đang mount "
                "read-only — sửa mount thành đọc-ghi hoặc chỉnh file trên host.")
    return None
