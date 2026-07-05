"""Chọn secret backend theo config (spec P0-P1 §3.2, P1-17).

Bất biến (chốt 2026-07-05): backend do config chọn phải THẬT SỰ chạy được — không
tụt xuống plaintext/file store một cách im lặng khi thư viện của backend đã chọn
thiếu ở runtime. Trường hợp đó: fail startup ngay với `UserFacingError` kèm
remediation cụ thể (đổi backend / cài lib) — caller (`cli.py`) đã bắt `YettError`
và in lỗi rồi thoát, nên "fail startup" ở đây là raise, không phải sys.exit trực tiếp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yett.errors import UserFacingError

if TYPE_CHECKING:
    from yett.secrets.base import SecretStore


def build_secret_store(backend: str) -> SecretStore:
    if backend == "env":
        from yett.secrets.backends import EnvSecretStore

        return EnvSecretStore()
    if backend == "file":
        from yett.secrets.file_store import FileSecretStore

        return FileSecretStore()
    if backend == "keyring":
        from yett.secrets.backends import KeyringSecretStore

        # KeyringSecretStore.__init__ tự raise UserFacingError nếu thiếu lib `keyring`
        # (fail startup có remediation) — không bắt lỗi ở đây để giữ message rõ ràng.
        return KeyringSecretStore()
    if backend == "age":
        # [P1-17] age: GIỮ trong enum (đã chốt dùng — Q2/Q3) nhưng CHƯA implement.
        # Chọn "age" phải fail startup rõ ràng, không im lặng tụt về plaintext.
        raise UserFacingError(
            "secret_backend=age chưa được hỗ trợ trong bản này (chưa implement). "
            "Đổi secret_backend sang 'keyring', 'env', hoặc 'file' trong config."
        )
    raise UserFacingError(
        f"secret_backend '{backend}' không hợp lệ. Giá trị hỗ trợ: keyring, env, file "
        "(age đã khai báo nhưng chưa implement)."
    )
