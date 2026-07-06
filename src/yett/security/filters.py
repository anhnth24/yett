"""Result Filters (spec P0-P1 §3.5). Redact secret/PII + quét prompt-injection trên tool result.

Chạy SAU khi tool thực thi, TRƯỚC khi result vào context. Cũng dùng để redact span attrs.
"""

from __future__ import annotations

import re
from typing import Any

from yett.tools.base import ToolResult

# Pattern secret phổ biến (mở rộng dần). Redact = thay bằng [REDACTED] (giữ group
# ngữ cảnh nếu replacement có backreference, vd `\1=[REDACTED]`).
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "[REDACTED]"),
    # [RT-6/19][M2] generic: khớp mọi key dạng sk-<provider->...  (vd sk-proj-, sk-cp-
    # — format key MiniMax đã lộ/rotate). `\b` trước "sk-" tránh khớp giữa từ
    # (vd "desk-top-..."); sàn 20 ký tự sau "sk-" tránh khớp cụm ngắn vô hại. Alphabet gồm cả
    # '_' và '.' vì key provider thật dùng chúng (vd `sk-cp-...._....`) — pattern cũ chỉ
    # `[A-Za-z0-9-]` đứt ở '_'/'.' làm key lọt (M2 từ codex review).
    ("generic_sk_key", re.compile(r"\bsk-[A-Za-z0-9._-]{20,}"), "[REDACTED]"),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{20,}"), "[REDACTED]"),
    ("pw_in_dsn", re.compile(r"(?i)(://[^:/@\s]+:)[^@/\s]+(@)"), r"\1[REDACTED]\2"),
    # ODBC/ADO DSN: `Pwd=...;` / `Password=...;` — giữ tên key, redact giá trị. Giá trị có thể
    # là bare (dừng ở ';'), quote đơn/kép, hoặc bọc `{...}` (ODBC cho phép ký tự đặc biệt kể cả
    # ';' bên trong) — nếu chỉ khớp bare thì `Pwd='se;cret'`/`Pwd={p@ss;word}` lọt một phần.
    (
        "odbc_dsn_pwd",
        re.compile(r"(?i)\b(pwd|password)\s*=\s*('[^']*'|\"[^\"]*\"|\{[^}]*\}|[^;'\"\s]+)"),
        r"\1=[REDACTED]",
    ),
]

# [P0-5][RT-15] Private key PEM block: redact TOÀN KHỐI, không chỉ header.
# (1) Block đầy đủ có footer END — DOTALL để khớp qua nhiều dòng, non-greedy + backref
#     \1 để không nuốt qua block PEM kế tiếp (vd 2 key liên tiếp trong 1 output).
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----",
    re.DOTALL,
)
# (2) Fallback khi output bị cắt (head -c, log phân trang, exec cap size) → mất
#     footer END. Heuristic: có header BEGIN...PRIVATE KEY nhưng không tìm được
#     END tương ứng (vì (1) đã xử lý hết các block trọn vẹn) ⇒ coi toàn bộ phần
#     còn lại sau header là thân base64 của key bị lộ, redact tới hết chuỗi.
_PRIVATE_KEY_TRUNCATED = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*",
    re.DOTALL,
)

# Dấu hiệu prompt-injection trên nội dung từ ngoài (web/file).
_INJECTION = re.compile(
    r"(?i)(ignore (all |your |the |previous )*(instructions|prompt)"
    r"|disregard (the |all |previous |above )*(above|previous|instructions)"
    r"|you are now|new instructions:|system prompt)"
)


def _redact_private_keys(text: str) -> str:
    """Redact PEM private key: block trọn vẹn trước, phần bị cắt (thiếu footer) sau."""
    text = _PRIVATE_KEY_BLOCK.sub("[REDACTED]", text)
    text = _PRIVATE_KEY_TRUNCATED.sub("[REDACTED]", text)
    return text


def redact(text: str) -> str:
    text = _redact_private_keys(text)
    for _name, pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _redact_value(value: Any) -> Any:  # noqa: ANN401 — giá trị span attrs là JSON-like động
    """Redact đệ quy: dict/list lồng bao nhiêu cấp cũng bị quét (P1-7)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def redact_attrs(attrs: dict) -> dict:
    """Redact đệ quy mọi giá trị chuỗi trong span attrs, kể cả dict/list lồng (RG1-9, P1-7)."""
    return {k: _redact_value(v) for k, v in attrs.items()}


def scan_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def apply(result: ToolResult, *, untrusted: bool = False) -> ToolResult:
    """Lọc một tool result trước khi vào context."""
    content = redact(result.content)
    if untrusted and scan_injection(content):
        content = (
            "[⚠️ nội dung từ nguồn ngoài chứa dấu hiệu prompt-injection — đã cách ly]\n"
            + content
        )
    return ToolResult(ok=result.ok, content=content, is_error=result.is_error)
