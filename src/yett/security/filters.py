"""Result Filters (spec P0-P1 §3.5). Redact secret/PII + quét prompt-injection trên tool result.

Chạy SAU khi tool thực thi, TRƯỚC khi result vào context. Cũng dùng để redact span attrs.
"""

from __future__ import annotations

import re

from yett.tools.base import ToolResult

# Pattern secret phổ biến (mở rộng dần). Redact = thay bằng [REDACTED:<loại>].
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{20,}")),
    ("pw_in_dsn", re.compile(r"(?i)(://[^:/@\s]+:)[^@/\s]+(@)")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# Dấu hiệu prompt-injection trên nội dung từ ngoài (web/file).
_INJECTION = re.compile(
    r"(?i)(ignore (all |your |the |previous )*(instructions|prompt)"
    r"|disregard (the |all |previous |above )*(above|previous|instructions)"
    r"|you are now|new instructions:|system prompt)"
)


def redact(text: str) -> str:
    for name, pat in _SECRET_PATTERNS:
        if name == "pw_in_dsn":
            text = pat.sub(r"\1[REDACTED]\2", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


def redact_attrs(attrs: dict) -> dict:
    """Redact mọi giá trị chuỗi trong span attrs (RG1-9)."""
    out = {}
    for k, v in attrs.items():
        out[k] = redact(v) if isinstance(v, str) else v
    return out


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
