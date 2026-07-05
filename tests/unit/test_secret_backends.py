"""Test KeyringSecretStore (P1-17, spec P0-P1 §3.2).

Mock hoàn toàn API module-level `keyring` (get_password/set_password) qua
`sys.modules` — KHÔNG phụ thuộc OS credential manager thật (Windows Credential
Manager/macOS Keychain/Linux Secret Service), nên test chạy deterministic trên
mọi CI leg (kể cả headless Linux không có secret service).

Bất biến cần verify (quyết định 2026-07-05): backend `keyring` chọn nhưng thư viện
thiếu ở runtime → fail startup có remediation, KHÔNG tụt xuống plaintext im lặng.
"""

from __future__ import annotations

import sys

import pytest

from yett.errors import SecretNotFound, SystemError, UserFacingError
from yett.secrets.backends import KeyringSecretStore
from yett.secrets.resolve import build_secret_store


class _FakeKeyringModule:
    """Giả lập API module-level của lib `keyring` bằng in-memory dict."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}
        self.set_password_calls: list[tuple[str, str, str]] = []
        self.get_password_calls: list[tuple[str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.get_password_calls.append((service, username))
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_password_calls.append((service, username, password))
        self._store[(service, username)] = password


class _RaisingKeyringModule:
    """Giả lập keyring mà backend OS bên dưới lỗi (KeyringLocked/PasswordSetError...)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_password(self, service: str, username: str) -> str | None:
        raise self._exc

    def set_password(self, service: str, username: str, password: str) -> None:
        raise self._exc


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyringModule:
    """`import keyring as _keyring` bên trong backends.py sẽ lấy đúng object này vì
    Python import system dùng thẳng `sys.modules["keyring"]` nếu đã có sẵn."""
    fake = _FakeKeyringModule()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def test_keyring_store_set_then_get_roundtrip(fake_keyring: _FakeKeyringModule) -> None:
    store = KeyringSecretStore()
    store.set("llm_key", "sk-test-abc123")
    assert store.get("llm_key") == "sk-test-abc123"
    # gọi đúng OS credential manager qua service "yett" (không tự bịa service khác)
    assert fake_keyring.set_password_calls == [("yett", "llm_key", "sk-test-abc123")]
    assert fake_keyring.get_password_calls == [("yett", "llm_key")]


def test_keyring_store_get_missing_raises_secret_not_found(
    fake_keyring: _FakeKeyringModule,
) -> None:
    store = KeyringSecretStore()
    with pytest.raises(SecretNotFound):
        store.get("khong-ton-tai")


def test_keyring_store_custom_service_name(fake_keyring: _FakeKeyringModule) -> None:
    store = KeyringSecretStore(service="my-app")
    store.set("db_pw", "hunter2")
    assert fake_keyring.set_password_calls == [("my-app", "db_pw", "hunter2")]


def test_keyring_store_get_backend_error_raises_secret_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keyring.errors import KeyringLocked

    monkeypatch.setitem(sys.modules, "keyring", _RaisingKeyringModule(KeyringLocked("locked")))
    store = KeyringSecretStore()
    with pytest.raises(SecretNotFound):
        store.get("llm_key")


def test_keyring_store_set_backend_error_raises_system_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keyring.errors import PasswordSetError

    monkeypatch.setitem(sys.modules, "keyring", _RaisingKeyringModule(PasswordSetError("boom")))
    store = KeyringSecretStore()
    with pytest.raises(SystemError):
        store.set("llm_key", "value")


def test_keyring_store_missing_library_fails_startup_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P1-17] Thư viện `keyring` thiếu ở runtime → UserFacingError NGAY khi khởi tạo,
    KHÔNG tụt xuống plaintext im lặng. Remediation phải gợi ý cài lib hoặc đổi backend."""
    monkeypatch.setitem(sys.modules, "keyring", None)  # mô phỏng ImportError thật
    with pytest.raises(UserFacingError, match="keyring"):
        KeyringSecretStore()


def test_build_secret_store_keyring_wires_keyring_backend(fake_keyring: _FakeKeyringModule) -> None:
    store = build_secret_store("keyring")
    assert isinstance(store, KeyringSecretStore)
    store.set("k", "v")
    assert fake_keyring.set_password_calls == [("yett", "k", "v")]


def test_build_secret_store_keyring_missing_library_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(UserFacingError):
        build_secret_store("keyring")


def test_build_secret_store_age_not_implemented_fails_startup_with_remediation() -> None:
    """[P1-17] age GIỮ trong enum (đã chốt dùng) nhưng chưa implement — chọn phải
    fail startup rõ ràng, không im lặng tụt về backend khác."""
    with pytest.raises(UserFacingError, match="age"):
        build_secret_store("age")


def test_build_secret_store_env_and_file_still_work() -> None:
    """Không phá vỡ 2 backend hiện có khi thêm keyring/age."""
    from yett.secrets.backends import EnvSecretStore
    from yett.secrets.file_store import FileSecretStore

    assert isinstance(build_secret_store("env"), EnvSecretStore)
    assert isinstance(build_secret_store("file"), FileSecretStore)
