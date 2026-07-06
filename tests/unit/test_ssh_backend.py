"""Test AsyncSSHBackend/`_connect_target` (spec P2 §2.3, phase 7b hardening):
- không còn `known_hosts=None` (tắt hẳn verify host key) — mặc định KHÔNG set tham số
  này (asyncssh dùng known_hosts hệ thống); set khi HostProfile pin known_hosts riêng.
- port/user đọc đúng từ HostProfile (P2 report: SSH luôn cắm cứng port 22).

`asyncssh` không phải dependency bắt buộc (lazy import, optional) và KHÔNG có trong môi
trường test — dùng module giả qua `sys.modules` để test hành vi build kwargs mà không
cần cài package thật.
"""

from __future__ import annotations

import sys
import types

import pytest

from yett.tools.remote.hostprofile import HostProfile
from yett.tools.remote.ssh_backend import AsyncSSHBackend, _connect_target


# --- _connect_target: build kwargs thuần, không cần asyncssh ---
def test_connect_target_default_port_omits_known_hosts() -> None:
    host = HostProfile(address="10.0.0.1", auth="keyfile:k")
    addr, kwargs = _connect_target(host)
    assert addr == "10.0.0.1"
    assert kwargs["port"] == 22
    # KHÔNG được set known_hosts=None (lỗ hổng cũ) — mặc định không set gì cả.
    assert "known_hosts" not in kwargs


def test_connect_target_custom_port() -> None:
    host = HostProfile(address="10.0.0.1", auth="keyfile:k", port=2222)
    _, kwargs = _connect_target(host)
    assert kwargs["port"] == 2222


def test_connect_target_pins_known_hosts_when_set() -> None:
    host = HostProfile(address="10.0.0.1", auth="keyfile:k", known_hosts="/etc/yett/known_hosts_uat")
    _, kwargs = _connect_target(host)
    assert kwargs["known_hosts"] == "/etc/yett/known_hosts_uat"


def test_connect_target_parses_user_from_address() -> None:
    host = HostProfile(address="deploy@10.0.0.1", auth="keyfile:k")
    addr, kwargs = _connect_target(host)
    assert addr == "10.0.0.1"
    assert kwargs["username"] == "deploy"


def test_connect_target_no_user_omits_username() -> None:
    host = HostProfile(address="10.0.0.1", auth="keyfile:k")
    _, kwargs = _connect_target(host)
    assert "username" not in kwargs


# --- AsyncSSHBackend.run end-to-end với module asyncssh giả ---
class _FakeResult:
    exit_status = 0
    stdout = "ok\n"
    stderr = ""


class _FakeConn:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    async def run(self, cmd: str, check: bool = False) -> _FakeResult:
        self._captured["cmd"] = cmd
        return _FakeResult()


class _FakeConnectCM:
    """asyncssh.connect() KHÔNG await trước `async with` — trả thẳng 1 object hỗ trợ
    async context manager (giống asyncssh thật), không phải coroutine cần await."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _install_fake_asyncssh(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    def fake_connect(addr: str, **kwargs: object) -> _FakeConnectCM:
        captured["addr"] = addr
        captured["kwargs"] = kwargs
        return _FakeConnectCM(_FakeConn(captured))

    fake_mod = types.ModuleType("asyncssh")
    fake_mod.connect = fake_connect  # type: ignore[attr-defined]
    fake_mod.import_private_key = lambda k: f"key:{k}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncssh", fake_mod)


async def test_backend_uses_configured_port_user_and_known_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _install_fake_asyncssh(monkeypatch, captured)
    host = HostProfile(
        address="deploy@10.0.0.9", auth="keyfile:k", port=2222,
        known_hosts="/etc/yett/known_hosts_uat",
    )
    backend = AsyncSSHBackend()
    code, out, err = await backend.run(host, "uptime", key="PRIVATE_KEY_DATA")

    assert captured["addr"] == "10.0.0.9"
    assert captured["kwargs"]["port"] == 2222  # [P2 report] không còn cắm cứng 22
    assert captured["kwargs"]["username"] == "deploy"
    assert captured["kwargs"]["known_hosts"] == "/etc/yett/known_hosts_uat"
    assert captured["kwargs"]["client_keys"] == ["key:PRIVATE_KEY_DATA"]
    assert code == 0 and out == "ok\n" and captured["cmd"] == "uptime"


async def test_backend_default_port_still_omits_known_hosts_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression cho lỗ hổng cũ: host không pin known_hosts thì asyncssh.connect KHÔNG
    được nhận `known_hosts=None` (tắt verify) — tham số này phải vắng mặt hoàn toàn."""
    captured: dict = {}
    _install_fake_asyncssh(monkeypatch, captured)
    host = HostProfile(address="10.0.0.1", auth="keyfile:k")
    backend = AsyncSSHBackend()
    await backend.run(host, "uptime", key="")

    assert captured["kwargs"]["port"] == 22
    assert "known_hosts" not in captured["kwargs"]
    assert "client_keys" not in captured["kwargs"]  # key rỗng -> không truyền


async def test_backend_missing_asyncssh_raises_helpful_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # asyncssh không phải dependency của dự án — môi trường test không cài nó thật,
    # nên import trong `run()` tự nhiên thất bại (không cần giả lập thêm).
    monkeypatch.delitem(sys.modules, "asyncssh", raising=False)
    host = HostProfile(address="10.0.0.1", auth="keyfile:k")
    backend = AsyncSSHBackend()
    with pytest.raises(RuntimeError, match="asyncssh"):
        await backend.run(host, "uptime", key="")
