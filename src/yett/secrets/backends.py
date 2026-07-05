"""Secret store backends (spec P0-P1 §3.2).

- EnvSecretStore: đọc từ biến môi trường (v0.1, đơn giản, không lộ ra đĩa).
- InMemorySecretStore: cho test.
- (keyring/age backend thêm khi cần — cùng interface SecretStore.)

Bất biến: giá trị secret KHÔNG bao giờ được log/ghi span/checkpoint.
"""

from __future__ import annotations

import os

from yett.errors import SecretNotFound


class EnvSecretStore:
    """Đọc secret từ env, tiền tố YETT_SECRET_<NAME uppercase>."""

    def __init__(self, prefix: str = "YETT_SECRET_") -> None:
        self._prefix = prefix

    def get(self, name: str) -> str:
        key = self._prefix + name.upper()
        val = os.environ.get(key)
        if val is None:
            raise SecretNotFound(f"secret '{name}' không tồn tại (env {key})")
        return val


class InMemorySecretStore:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._d = dict(secrets or {})

    def set(self, name: str, value: str) -> None:
        self._d[name] = value

    def get(self, name: str) -> str:
        if name not in self._d:
            raise SecretNotFound(f"secret '{name}' không tồn tại")
        return self._d[name]
