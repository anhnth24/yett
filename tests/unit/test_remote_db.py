"""Test remote ops + DB tools với backend fake (RG2 S3/S4/S5).

Bảo đảm: host lạ → deny, log ngoài log_paths → deny, secret không lộ, DB write → deny.
"""

from __future__ import annotations

from yett.secrets.backends import InMemorySecretStore
from yett.tools.db.db_query import DbProfile, DbQueryTool
from yett.tools.remote.hostprofile import HostProfile, HostRegistry
from yett.tools.remote.ssh_exec import LogReadTool, SshExecTool
from yett.tools.remote.vpn import VpnManager, VpnTool


class _Ctx:
    session_key = "t"


class _FakeSsh:
    def __init__(self) -> None:
        self.last_key = None
        self.last_cmd = None

    async def run(self, host, cmd, *, key):
        self.last_key = key
        self.last_cmd = cmd
        return (0, f"output of: {cmd}", "")


class _FakeVpnRunner:
    def __init__(self) -> None:
        self.connected = set()

    async def connect(self, profile, *, creds):
        self.connected.add(profile)
        return True

    async def disconnect(self, profile):
        self.connected.discard(profile)
        return True

    async def status(self, profile):
        return profile in self.connected


def _hosts() -> HostRegistry:
    return HostRegistry({
        "uat-app-1": HostProfile(
            address="10.0.0.1", auth="keyfile:uat_key", vpn_required="office",
            log_paths=["/var/log/app/*.log"], deploy_script="/opt/deploy/run.sh",
        )
    })


async def test_ssh_host_unknown_denied() -> None:
    import pytest

    from yett.errors import UserFacingError

    secrets = InMemorySecretStore({"uat_key": "PRIVATEKEY"})
    tool = SshExecTool(_hosts(), _FakeSsh(), secrets)
    # host lạ → UserFacingError (wiring biến thành ToolResult.error; ở unit kiểm exception)
    with pytest.raises(UserFacingError):
        await tool.run({"host": "unknown", "cmd": "tail x"}, _Ctx())


async def test_ssh_uses_key_from_secret_not_context() -> None:
    secrets = InMemorySecretStore({"uat_key": "PRIVATEKEY"})
    ssh = _FakeSsh()
    vpn = VpnManager(_FakeVpnRunner(), secrets, {"office": {}})
    tool = SshExecTool(_hosts(), ssh, secrets, vpn=vpn)
    res = await tool.run({"host": "uat-app-1", "cmd": "tail -n 10 /var/log/app/x.log"}, _Ctx())
    assert not res.is_error
    # key được truyền tới backend nhưng KHÔNG nằm trong kết quả trả về agent
    assert ssh.last_key == "PRIVATEKEY"
    assert "PRIVATEKEY" not in res.content


async def test_log_read_outside_paths_denied() -> None:
    secrets = InMemorySecretStore({"uat_key": "K"})
    tool = LogReadTool(_hosts(), _FakeSsh(), secrets, vpn=VpnManager(_FakeVpnRunner(), secrets, {"office": {}}))
    res = await tool.run({"host": "uat-app-1", "path": "/etc/passwd"}, _Ctx())
    assert res.is_error and "DENIED" in res.content


async def test_log_read_allowed_path() -> None:
    secrets = InMemorySecretStore({"uat_key": "K"})
    tool = LogReadTool(_hosts(), _FakeSsh(), secrets, vpn=VpnManager(_FakeVpnRunner(), secrets, {"office": {}}))
    res = await tool.run({"host": "uat-app-1", "path": "/var/log/app/error.log"}, _Ctx())
    assert not res.is_error


async def test_vpn_connect_disconnect() -> None:
    secrets = InMemorySecretStore({"vpn_pw": "secret"})
    runner = _FakeVpnRunner()
    mgr = VpnManager(runner, secrets, {"office": {"cred_secret": "vpn_pw"}})
    tool = VpnTool(mgr)
    r = await tool.run({"action": "connect", "profile": "office"}, _Ctx())
    assert "kết nối" in r.content
    assert "office" in runner.connected
    r2 = await tool.run({"action": "status", "profile": "office"}, _Ctx())
    assert "connected" in r2.content


# --- DB ---
class _FakeDb:
    async def query_readonly(self, dsn, driver, sql):
        assert "readonly-dsn" in dsn
        return [{"id": 123, "status": "PAID"}]


def _db_profiles():
    return {"uat": DbProfile(driver="postgres", dsn_secret="uat_dsn")}


async def test_db_select_allowed() -> None:
    secrets = InMemorySecretStore({"uat_dsn": "postgres://readonly-dsn"})
    tool = DbQueryTool(_db_profiles(), _FakeDb(), secrets)
    res = await tool.run({"profile": "uat", "sql": "SELECT * FROM orders WHERE id=123"}, _Ctx())
    assert not res.is_error and "123" in res.content


async def test_db_write_denied() -> None:
    secrets = InMemorySecretStore({"uat_dsn": "postgres://readonly-dsn"})
    tool = DbQueryTool(_db_profiles(), _FakeDb(), secrets)
    res = await tool.run({"profile": "uat", "sql": "UPDATE orders SET status='X' WHERE id=1"}, _Ctx())
    assert res.is_error and "DENIED" in res.content


async def test_db_dsn_not_in_result() -> None:
    secrets = InMemorySecretStore({"uat_dsn": "postgres://readonly-dsn:pw@host/db"})
    tool = DbQueryTool(_db_profiles(), _FakeDb(), secrets)
    res = await tool.run({"profile": "uat", "sql": "SELECT id FROM orders"}, _Ctx())
    assert "readonly-dsn" not in res.content  # connection string không lộ ra agent
