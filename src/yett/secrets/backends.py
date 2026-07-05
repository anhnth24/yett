"""Secret store backends (spec P0-P1 §3.2).

- EnvSecretStore: đọc từ biến môi trường (v0.1, đơn giản, không lộ ra đĩa).
- InMemorySecretStore: cho test.
- KeyringSecretStore: mã hoá tại chỗ qua OS credential manager (P1-17).
- (age backend: CHƯA implement — xem `resolve.py`, chọn "age" fail startup có remediation.)

Bất biến: giá trị secret KHÔNG bao giờ được log/ghi span/checkpoint.
"""

from __future__ import annotations

import os

from yett.errors import SecretNotFound, SystemError, UserFacingError


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


class KeyringSecretStore:
    """Secret at-rest qua OS credential manager (P1-17, Q2/Q3 chốt 2026-07-05):
    Windows Credential Manager / macOS Keychain / Linux Secret Service, dùng lib
    `keyring`. Mã hoá tại chỗ do OS quản lý — KHÔNG tự build vault, không thêm hạ tầng.

    Fail-loud: nếu thư viện `keyring` chưa cài ở runtime → UserFacingError NGAY khi
    khởi tạo (fail startup có remediation rõ), KHÔNG tụt xuống plaintext im lặng
    (quyết định 2026-07-05, xem plan.md Phase 5).
    """

    _SERVICE = "yett"

    def __init__(self, service: str | None = None) -> None:
        try:
            import keyring as _keyring
        except ImportError as e:
            raise UserFacingError(
                "secret_backend=keyring nhưng thư viện 'keyring' chưa được cài trong "
                "môi trường này. Cài bằng `pip install keyring`, hoặc đổi secret_backend "
                "sang 'env' hoặc 'file' trong config."
            ) from e
        self._keyring = _keyring
        self._service = service or self._SERVICE

    def get(self, name: str) -> str:
        try:
            value = self._keyring.get_password(self._service, name)
        except Exception as e:
            # Bắt rộng (không chỉ keyring.errors.KeyringError): OS credential manager có
            # thể lỗi theo nhiều cách (khoá, thiếu daemon, quyền truy cập) — theo bất
            # biến fail-closed của module secret (xem errors.py: "mọi lỗi → fail-closed"),
            # mọi lỗi đọc secret đều quy về SecretNotFound thay vì rò rỉ exception lạ.
            raise SecretNotFound(f"secret '{name}' lỗi khi đọc từ keyring: {e}") from e
        if value is None:
            raise SecretNotFound(
                f"secret '{name}' không tồn tại trong keyring (service={self._service})"
            )
        return value

    def set(self, name: str, value: str) -> None:
        try:
            self._keyring.set_password(self._service, name, value)
        except Exception as e:  # xem giải thích bắt rộng ở get()
            raise SystemError(f"không ghi được secret '{name}' vào keyring: {e}") from e
