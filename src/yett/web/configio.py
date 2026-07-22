"""Đọc/ghi config qua web UI (xem + sửa). Validate bằng pydantic trước khi lưu (fail-closed)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from yett.config.models import HarnessCfg

# Phải khớp replacement trong yett.security.filters (mọi pattern redact về cùng chuỗi này).
_REDACT_MARKER = "[REDACTED]"
_KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):[ \t]*(.*?)\s*$")

# Field inline chứa secret THẬT (che theo TÊN field, không dựa vào định dạng value — vì
# redactor theo pattern chỉ bắt sk-…, bỏ sót key GLM `id.secret` là provider mặc định).
# Reference fields such as api_key_secret/dsn_secret are names, not values. These fields
# contain bearer credentials directly and must never be returned by GET /api/config.
_SECRET_FIELDS = frozenset({"api_key", "pairing_code", "webhook_secret"})


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
    full mapping path). Giữ nguyên comment/format bản gửi lên; chỉ đụng dòng có marker.

    Không được chỉ khớp `(indent, key)`: `channels.telegram.pairing_code` và
    `channels.zalo.pairing_code` có cùng indent/key. Nếu người dùng xóa hoặc đổi thứ tự một
    block, matching theo occurrence sẽ gắn secret của kênh này sang kênh kia. Người dùng
    muốn ĐỔI secret vẫn gõ giá trị mới (không còn marker) như thường.
    """
    if _REDACT_MARKER not in submitted_text:
        return submitted_text
    original_vals: dict[tuple[str, ...], list[str]] = {}
    for line, path in _mapping_paths(original_text):
        m = _KV_RE.match(line)
        if m and _REDACT_MARKER not in m.group(3):
            original_vals.setdefault(path, []).append(m.group(3))
    out: list[str] = []
    submitted_occurrences: dict[tuple[str, ...], int] = {}
    for line, path in _mapping_paths(submitted_text):
        m = _KV_RE.match(line)
        if m:
            index = submitted_occurrences.get(path, 0)
            submitted_occurrences[path] = index + 1
            originals = original_vals.get(path, [])
            if _REDACT_MARKER in m.group(3) and index < len(originals):
                out.append(f"{m.group(1)}{m.group(2)}: {originals[index]}")
                continue
        out.append(line)
    joined = "\n".join(out)
    return joined + "\n" if submitted_text.endswith("\n") else joined


def _mapping_paths(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return scalar/mapping lines with their indentation-derived YAML mapping paths.

    Harness secret-bearing fields are mappings rather than sequence entries. Syntax and
    schema validation still run after restoration; this helper only prevents ambiguous
    cross-block secret substitution while preserving the submitted formatting/comments.
    """
    stack: list[tuple[int, str]] = []
    out: list[tuple[str, tuple[str, ...]]] = []
    for line in text.splitlines():
        match = _KV_RE.match(line)
        if match is None:
            out.append((line, ()))
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = tuple(key for _, key in stack) + (match.group(2),)
        out.append((line, path))
        stack.append((indent, match.group(2)))
    return out


def validate_config_text(text: str) -> str | None:
    """Trả None nếu hợp lệ; ngược lại trả thông báo lỗi (không lưu nếu lỗi)."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return f"YAML lỗi cú pháp: {e}"
    try:
        HarnessCfg.model_validate(raw)
    except ValidationError as e:
        # Pydantic's default string includes input_value, which may be a webhook/pairing
        # secret. Return only field locations and validator messages.
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in e.errors(include_input=False)
        )
        return f"Config không hợp lệ: {details}"
    except Exception as e:  # noqa: BLE001 — lỗi không-Pydantic, không kèm config input
        return f"Config không hợp lệ: {type(e).__name__}"
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
