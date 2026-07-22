"""Adversarial / offline tests for SubprocessVpnRunner + VPN wiring.

Không gọi openfortivpn/openvpn thật — commander injectable. Bảo đảm: argv-only, allowlist,
không secret trong lỗi/result, cleanup temp, ownership disconnect, timeout, missing binary,
injection/traversal/symlink, duplicate connect, redaction.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from yett.config.models import SecurityCfg, VpnProfileCfg
from yett.core.cancel import CancelToken, Cancelled
from yett.errors import UserFacingError
from yett.secrets.backends import InMemorySecretStore
from yett.security.basic_gate import BasicGate
from yett.tools.remote.vpn import (
    OwnedProcess,
    SubprocessVpnRunner,
    VpnManager,
    VpnTool,
    _cleanup_paths,
    _open_config_nofollow,
    _write_secret_temp,
)
from yett.tools.remote import vpn as vpn_module


class _Ctx:
    session_key = "t"


class _FakeProc:
    def __init__(self, *, exit_after: float | None = None, exit_code: int = 1) -> None:
        self.returncode: int | None = None
        self.pid = 4242
        self._exit_after = exit_after
        self._exit_code = exit_code
        self._term = asyncio.Event()
        self.stdin = None
        self.stdout = None
        self.stderr = None
        if exit_after is not None:
            asyncio.get_event_loop().call_later(exit_after, self._expire)

    def _expire(self) -> None:
        if self.returncode is None:
            self.returncode = self._exit_code
            self._term.set()

    def terminate(self) -> None:
        self.returncode = -15
        self._term.set()

    def kill(self) -> None:
        self.returncode = -9
        self._term.set()

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        await self._term.wait()
        return int(self.returncode or 0)


class _FakeCommander:
    """Ghi lại argv; mô phỏng process sống (connected) hoặc thoát sớm / timeout."""

    def __init__(
        self,
        *,
        mode: str = "up",
        which_map: dict[str, str] | None = None,
        marker: bytes = b"Tunnel is up and running.\n",
    ) -> None:
        self.calls: list[list[str]] = []
        self.stdins: list[bytes | None] = []
        self.mode = mode
        self.which_map = (
            {
                "openfortivpn": "/usr/sbin/openfortivpn",
                "openvpn": "/usr/sbin/openvpn",
            }
            if which_map is None
            else which_map
        )
        self.marker = marker
        self.started: list[OwnedProcess] = []

    def which(self, name: str) -> str | None:
        return self.which_map.get(name)

    async def start(self, argv, *, stdin=None, env=None) -> OwnedProcess:
        self.calls.append(list(argv))
        self.stdins.append(stdin)
        # Đọc file cred ngay (giống CLI thật) rồi caller có thể xoá.
        for i, a in enumerate(argv):
            if a in ("-c", "--auth-user-pass", "--config") and i + 1 < len(argv):
                p = Path(argv[i + 1])
                if p.exists() and a != "--config":
                    _ = p.read_bytes()
        if self.mode == "missing_bin":
            raise FileNotFoundError("no such file")
        if self.mode == "fail_exit":
            proc = _FakeProc(exit_after=0.01, exit_code=1)
            owned = OwnedProcess(pid=proc.pid, argv0=argv[0], _proc=proc)
            owned._stderr_buf.extend(b"auth failed: password=SUPERSECRET\n")
            self.started.append(owned)
            return owned
        if self.mode == "hang":
            proc = _FakeProc(exit_after=None)
            owned = OwnedProcess(pid=proc.pid, argv0=argv[0], _proc=proc)
            self.started.append(owned)
            return owned
        # mode == "up": process sống + marker
        proc = _FakeProc(exit_after=None)
        owned = OwnedProcess(pid=proc.pid, argv0=argv[0], _proc=proc)
        owned._stdout_buf.extend(self.marker)
        self.started.append(owned)
        return owned


def _forti(**kwargs) -> VpnProfileCfg:
    base = dict(kind="openfortivpn", host="vpn.example.com", cred_secret="vpn_pw", username="alice")
    base.update(kwargs)
    return VpnProfileCfg(**base)


def _ovpn(config_file: str, **kwargs) -> VpnProfileCfg:
    base = dict(kind="openvpn", config_file=config_file, cred_secret="vpn_pw", username="alice")
    base.update(kwargs)
    return VpnProfileCfg(**base)


# --- helpers ---


def test_write_secret_temp_mode_and_cleanup(tmp_path: Path) -> None:
    path = _write_secret_temp(b"password=SUPERSECRET\n", suffix=".conf")
    assert path.exists()
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    parent = path.parent
    assert parent.name.startswith("yett-vpn-")
    assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700
    assert b"SUPERSECRET" in path.read_bytes()
    _cleanup_paths([path])
    assert not path.exists()
    assert not parent.exists()


def test_open_config_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.ovpn"
    real.write_text("client\n", encoding="utf-8")
    link = tmp_path / "link.ovpn"
    link.symlink_to(real)
    with pytest.raises(UserFacingError, match="symlink"):
        _open_config_nofollow(str(link))


def test_open_config_rejects_traversal() -> None:
    with pytest.raises(UserFacingError):
        _open_config_nofollow("/tmp/../etc/passwd")


def test_open_config_rejects_relative() -> None:
    with pytest.raises(UserFacingError):
        # validator on model also catches; helper itself rejects non-absolute
        _open_config_nofollow("relative.ovpn")


# --- runner lifecycle ---


async def test_connect_status_disconnect_forti() -> None:
    secrets = InMemorySecretStore({"vpn_pw": "SUPERSECRET"})
    cmd = _FakeCommander(mode="up")
    profiles = {"office": _forti()}
    runner = SubprocessVpnRunner(profiles, commander=cmd)
    mgr = VpnManager(runner, secrets, profiles)
    tool = VpnTool(mgr)
    r = await tool.run({"action": "connect", "profile": "office"}, _Ctx())
    assert not r.is_error and "kết nối" in r.content
    assert "SUPERSECRET" not in r.content
    assert cmd.calls, "must spawn"
    argv = cmd.calls[0]
    assert argv[0] == "/usr/sbin/openfortivpn"
    assert argv[1] == "-c"
    assert "SUPERSECRET" not in " ".join(argv)  # secret không trên argv
    # temp cleaned after successful connect
    assert not Path(argv[2]).exists()
    st = await tool.run({"action": "status", "profile": "office"}, _Ctx())
    assert "connected" in st.content
    d = await tool.run({"action": "disconnect", "profile": "office"}, _Ctx())
    assert "ngắt" in d.content
    st2 = await tool.run({"action": "status", "profile": "office"}, _Ctx())
    assert "disconnected" in st2.content


async def test_openvpn_auth_file_not_on_argv_as_secret(tmp_path: Path) -> None:
    ovpn = tmp_path / "client.ovpn"
    ovpn.write_text("client\n", encoding="utf-8")
    cmd = _FakeCommander(mode="up", marker=b"Initialization Sequence Completed\n")
    profiles = {"home": _ovpn(str(ovpn))}
    runner = SubprocessVpnRunner(profiles, commander=cmd)
    ok = await runner.connect("home", creds={"username": "alice", "password": "SUPERSECRET"})
    assert ok
    argv = cmd.calls[0]
    assert argv[0].endswith("openvpn")
    assert "--config" in argv and str(ovpn) not in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    config_copy = argv[argv.index("--config") + 1]
    assert not Path(config_copy).exists()
    assert "--auth-user-pass" in argv
    joined = " ".join(argv)
    assert "SUPERSECRET" not in joined
    # auth file cleaned
    auth = argv[argv.index("--auth-user-pass") + 1]
    assert not Path(auth).exists()


async def test_profile_allowlist_rejects_unknown() -> None:
    cmd = _FakeCommander()
    runner = SubprocessVpnRunner({"office": _forti()}, commander=cmd)
    with pytest.raises(UserFacingError) as ei:
        await runner.connect("evil;rm -rf /", creds={"password": "x"})
    assert "hợp lệ" in str(ei.value)
    with pytest.raises(UserFacingError) as ei2:
        await runner.connect("other", creds={"password": "x"})
    assert "allowlist" in str(ei2.value)
    assert not cmd.calls


async def test_injection_in_profile_name_rejected() -> None:
    secrets = InMemorySecretStore({"vpn_pw": "pw"})
    mgr = VpnManager(
        SubprocessVpnRunner({"office": _forti()}, commander=_FakeCommander()),
        secrets,
        {"office": _forti()},
    )
    tool = VpnTool(mgr)
    for bad in ("office;id", "../office", "office`id`", "office\nstatus", ""):
        tool.validate({"action": "connect", "profile": "office"})  # ok baseline
        with pytest.raises(UserFacingError):
            tool.validate({"action": "connect", "profile": bad})


async def test_gate_rejects_unknown_profile_and_extra_args() -> None:
    gate = BasicGate(SecurityCfg(), vpn_profiles={"office"})
    ctx = _Ctx()
    d = gate.evaluate("vpn", {"action": "connect", "profile": "nope"}, ctx)
    assert d.verdict == "deny" and d.rule_id == "VPN_UNKNOWN_PROFILE"
    d2 = gate.evaluate(
        "vpn",
        {"action": "connect", "profile": "office", "extra_flags": "--script-security"},
        ctx,
    )
    assert d2.verdict == "deny" and d2.rule_id == "VPN_EXTRA_ARGS"
    d3 = gate.evaluate("vpn", {"action": "connect", "profile": "office"}, ctx)
    assert d3.verdict == "need_approval"
    d4 = gate.evaluate("vpn", {"action": "status", "profile": "office"}, ctx)
    assert d4.verdict == "allow"
    d5 = gate.evaluate("vpn", {"action": "disconnect", "profile": "office"}, ctx)
    assert d5.verdict == "need_approval"


async def test_duplicate_connect_idempotent() -> None:
    cmd = _FakeCommander(mode="up")
    profiles = {"office": _forti()}
    runner = SubprocessVpnRunner(profiles, commander=cmd)
    assert await runner.connect("office", creds={"username": "a", "password": "p"})
    assert await runner.connect("office", creds={"username": "a", "password": "p"})
    assert len(cmd.calls) == 1  # không spawn lần 2


async def test_disconnect_without_ownership() -> None:
    runner = SubprocessVpnRunner({"office": _forti()}, commander=_FakeCommander())
    with pytest.raises(UserFacingError, match="không sở hữu"):
        await runner.disconnect("office")


async def test_timeout_kills_and_cleans(tmp_path: Path) -> None:
    cmd = _FakeCommander(mode="hang")  # no marker, never exits
    profiles = {"office": _forti(connect_timeout_sec=1)}
    runner = SubprocessVpnRunner(profiles, commander=cmd)
    # Override wait loop: hang mode never writes marker; with connect_timeout=1 and
    # fallback at 0.5*timeout may still succeed. Force very short and no marker path
    # by using empty marker kind — use openvpn hang without marker in stdout.
    ovpn = tmp_path / "c.ovpn"
    ovpn.write_text("client\n", encoding="utf-8")
    cmd = _FakeCommander(mode="hang", marker=b"")
    profiles = {"home": _ovpn(str(ovpn), connect_timeout_sec=1)}
    # Monkeypatch success marker absence + stable fallback: hang forever without marker
    # _wait_until_up returns True at 0.5*timeout if process alive. To test TIMEOUT kill,
    # we need process that doesn't hit success path — patch runner method.
    runner = SubprocessVpnRunner(profiles, commander=cmd)

    async def _never_up(proc, cfg, *, cancel=None):
        deadline = asyncio.get_event_loop().time() + float(cfg.connect_timeout_sec)
        while asyncio.get_event_loop().time() < deadline:
            if cancel:
                cancel.check()
            await asyncio.sleep(0.05)
        return False

    runner._wait_until_up = _never_up  # type: ignore[method-assign]
    ok = await runner.connect("home", creds={"username": "a", "password": "SECRET_TIMEOUT"})
    assert ok is False
    assert "home" not in runner._sessions
    # temp auth cleaned
    argv = cmd.calls[0]
    auth = argv[argv.index("--auth-user-pass") + 1]
    assert not Path(auth).exists()


async def test_cancel_during_connect(tmp_path: Path) -> None:
    ovpn = tmp_path / "c.ovpn"
    ovpn.write_text("client\n", encoding="utf-8")
    cmd = _FakeCommander(mode="hang", marker=b"")
    runner = SubprocessVpnRunner(
        {"home": _ovpn(str(ovpn), connect_timeout_sec=30)}, commander=cmd
    )
    token = CancelToken()

    async def _cancel_soon():
        await asyncio.sleep(0.05)
        token.cancel()

    asyncio.create_task(_cancel_soon())
    with pytest.raises(Cancelled):
        await runner.connect(
            "home", creds={"username": "a", "password": "SECRET_CANCEL"}, cancel=token
        )
    assert "home" not in runner._sessions


async def test_missing_binary() -> None:
    cmd = _FakeCommander(which_map={})  # which returns None
    runner = SubprocessVpnRunner({"office": _forti()}, commander=cmd)
    with pytest.raises(UserFacingError, match="không tìm thấy lệnh"):
        await runner.connect("office", creds={"password": "x"})


async def test_command_failure_no_secret_in_error() -> None:
    secrets = InMemorySecretStore({"vpn_pw": "SUPERSECRET"})
    cmd = _FakeCommander(mode="fail_exit")
    profiles = {"office": _forti()}
    mgr = VpnManager(SubprocessVpnRunner(profiles, commander=cmd), secrets, profiles)
    tool = VpnTool(mgr)
    r = await tool.run({"action": "connect", "profile": "office"}, _Ctx())
    assert r.is_error
    assert "SUPERSECRET" not in r.content
    assert "password=" not in r.content.lower() or "[REDACTED]" in r.content


async def test_creds_cleared_after_ensure() -> None:
    secrets = InMemorySecretStore({"vpn_pw": "SUPERSECRET"})
    profiles = {"office": _forti()}
    runner = SubprocessVpnRunner(profiles, commander=_FakeCommander())
    mgr = VpnManager(runner, secrets, profiles)
    # Hook connect to capture creds dict identity
    captured: list[dict] = []
    orig = runner.connect

    async def wrap(profile, *, creds, cancel=None):
        captured.append(creds)
        return await orig(profile, creds=creds, cancel=cancel)

    runner.connect = wrap  # type: ignore[method-assign]
    await mgr.ensure("office")
    assert captured and captured[0] == {}  # cleared after use


async def test_ssh_fail_closed_without_vpn_manager() -> None:
    from yett.tools.remote.hostprofile import HostProfile, HostRegistry
    from yett.tools.remote.ssh_exec import SshExecTool

    class _Ssh:
        async def run(self, host, cmd, *, key):
            return (0, "ok", "")

    hosts = HostRegistry({
        "uat": HostProfile(
            address="10.0.0.1", auth="keyfile:k", vpn_required="office",
        )
    })
    tool = SshExecTool(hosts, _Ssh(), InMemorySecretStore({"k": "KEY"}), vpn=None)
    with pytest.raises(UserFacingError, match="VPN chưa được cấu hình"):
        await tool.run({"host": "uat", "cmd": "true"}, _Ctx())


async def test_app_wires_vpn_and_rejects_missing_profile(tmp_path: Path) -> None:
    from yett.app import App
    from yett.config.models import (
        BudgetCfg,
        HarnessCfg,
        HostCfg,
        ProviderCfg,
        RemoteCfg,
        SandboxCfg,
        VpnProfileCfg,
    )
    from yett.provider.fake import FakeProvider, text_result

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="m", api_key="k"),
        budget=BudgetCfg(),
        sandbox=SandboxCfg(backend="local"),
        workspace_root=ws,
        remote=RemoteCfg(
            vpn_profiles={
                "office": VpnProfileCfg(
                    kind="openfortivpn", host="vpn.example.com", cred_secret="vpn_pw",
                    username="u",
                )
            },
            hosts={
                "uat": HostCfg(
                    address="10.0.0.1", auth="keyfile:k", vpn_required="office",
                )
            },
        ),
    )
    app = App(
        cfg, FakeProvider([text_result("hi")]), state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"vpn_pw": "pw", "k": "KEY"}),
    )
    try:
        assert "vpn" in app.registry.names()
        assert "ssh_exec" in app.registry.names()
        assert app.vpn_manager is not None
    finally:
        app.close()

    with pytest.raises(UserFacingError, match="vpn_required"):
        App(
            HarnessCfg(
                provider=ProviderCfg(name="fake", model="m", api_key="k"),
                sandbox=SandboxCfg(backend="local"),
                workspace_root=ws,
                remote=RemoteCfg(
                    hosts={
                        "uat": HostCfg(
                            address="10.0.0.1", auth="keyfile:k", vpn_required="missing",
                        )
                    },
                    vpn_profiles={
                        "office": VpnProfileCfg(
                            kind="openfortivpn", host="vpn.example.com",
                            cred_secret="vpn_pw", username="u",
                        )
                    },
                ),
            ),
            FakeProvider([text_result("hi")]),
            state_dir=tmp_path / "st2",
            secrets=InMemorySecretStore({"vpn_pw": "pw", "k": "KEY"}),
        )


def test_vpn_profile_cfg_forbids_extra_and_bad_host() -> None:
    with pytest.raises(Exception):
        VpnProfileCfg(
            kind="openfortivpn", host="vpn.example.com", cred_secret="x",
            extra_args="--script-security",  # type: ignore[call-arg]
        )
    with pytest.raises(Exception):
        VpnProfileCfg(kind="openfortivpn", host="vpn;rm -rf /", cred_secret="x")
    with pytest.raises(Exception):
        VpnProfileCfg(kind="openvpn", config_file="relative.ovpn", cred_secret="x")


def test_doctor_vpn_config_problems(tmp_path: Path) -> None:
    from yett.doctor import _vpn_config_problems

    cfg = tmp_path / "harness.yaml"
    cfg.write_text(
        "provider: {name: fake, model: m, api_key: k}\n"
        "workspace_root: .\n"
        "sandbox: {backend: local}\n"
        "remote:\n"
        "  vpn_profiles:\n"
        "    office:\n"
        "      kind: openfortivpn\n"
        "      host: vpn.example.com\n"
        "      cred_secret: vpn_pw\n"
        "      username: u\n",
        encoding="utf-8",
    )
    # Nếu binary thiếu → problem; nếu có trên máy CI thì có thể rỗng — chỉ kiểm hàm không crash
    problems = _vpn_config_problems(cfg)
    assert isinstance(problems, list)


async def test_concurrent_connect_is_serialized_and_spawns_once() -> None:
    class BlockingCommander(_FakeCommander):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.start_count = 0

        async def start(self, argv, *, stdin=None, env=None):
            self.start_count += 1
            self.entered.set()
            await self.release.wait()
            return await super().start(argv, stdin=stdin, env=env)

    cmd = BlockingCommander()
    runner = SubprocessVpnRunner({"office": _forti()}, commander=cmd)
    first = asyncio.create_task(
        runner.connect("office", creds={"username": "a", "password": "p"})
    )
    await cmd.entered.wait()
    second = asyncio.create_task(
        runner.connect("office", creds={"username": "a", "password": "p"})
    )
    await asyncio.sleep(0.05)
    assert cmd.start_count == 1
    cmd.release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert cmd.start_count == 1
    runner.close()


async def test_status_waits_for_connect_and_never_reports_connecting_as_up() -> None:
    class BlockingCommander(_FakeCommander):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def start(self, argv, *, stdin=None, env=None):
            self.entered.set()
            await self.release.wait()
            return await super().start(argv, stdin=stdin, env=env)

    cmd = BlockingCommander()
    runner = SubprocessVpnRunner({"office": _forti()}, commander=cmd)
    connecting = asyncio.create_task(
        runner.connect("office", creds={"username": "a", "password": "p"})
    )
    await cmd.entered.wait()
    status = asyncio.create_task(runner.status("office"))
    await asyncio.sleep(0.05)
    assert not status.done()
    cmd.release.set()
    assert await connecting is True
    assert await status is True
    runner.close()


async def test_process_liveness_without_readiness_marker_fails_closed() -> None:
    cmd = _FakeCommander(mode="hang")
    runner = SubprocessVpnRunner(
        {"office": _forti(connect_timeout_sec=1)},
        commander=cmd,
    )
    assert await runner.connect("office", creds={"username": "a", "password": "p"}) is False
    assert cmd.started[0].returncode() == -15
    assert not runner._sessions


async def test_asyncio_task_cancellation_kills_and_cleans_all_temp_files(
    tmp_path: Path,
) -> None:
    ovpn = tmp_path / "client.ovpn"
    ovpn.write_text("client\n", encoding="utf-8")
    cmd = _FakeCommander(mode="hang")
    runner = SubprocessVpnRunner(
        {"home": _ovpn(str(ovpn), connect_timeout_sec=30)},
        commander=cmd,
    )
    task = asyncio.create_task(
        runner.connect("home", creds={"username": "a", "password": "S; e\ncret".replace("\n", "")})
    )
    while not cmd.calls:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner._sessions
    assert cmd.started[0].returncode() is not None
    for option in ("--config", "--auth-user-pass"):
        path = Path(cmd.calls[0][cmd.calls[0].index(option) + 1])
        assert not path.exists()


async def test_manager_rechecks_dead_child_instead_of_trusting_connected_cache() -> None:
    cmd = _FakeCommander()
    profiles = {"office": _forti()}
    runner = SubprocessVpnRunner(profiles, commander=cmd)
    manager = VpnManager(runner, InMemorySecretStore({"vpn_pw": "pw"}), profiles)
    await manager.ensure("office")
    cmd.started[0]._proc.returncode = 1
    await manager.ensure("office")
    assert len(cmd.calls) == 2
    manager.close()


@pytest.mark.parametrize(
    "directive",
    [
        "up /tmp/evil",
        "--plugin=/tmp/evil.so",
        "config nested.ovpn",
        "management-client-user nobody",
        "script-security 2",
        "iproute /tmp/evil",
    ],
)
def test_openvpn_config_rejects_code_execution_and_recursive_directives(
    tmp_path: Path, directive: str
) -> None:
    ovpn = tmp_path / "unsafe.ovpn"
    ovpn.write_text(f"client\n{directive}\n", encoding="utf-8")
    with pytest.raises(UserFacingError, match="directive bị cấm"):
        _open_config_nofollow(str(ovpn))


def test_openvpn_config_rejects_world_writable_and_oversize(tmp_path: Path) -> None:
    writable = tmp_path / "writable.ovpn"
    writable.write_text("client\n", encoding="utf-8")
    writable.chmod(0o666)
    with pytest.raises(UserFacingError, match="user khác ghi"):
        _open_config_nofollow(str(writable))

    huge = tmp_path / "huge.ovpn"
    huge.write_bytes(b"#" * (1_048_576 + 1))
    with pytest.raises(UserFacingError, match="1 MiB"):
        _open_config_nofollow(str(huge))


def test_secret_temp_partial_write_failure_removes_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_dir = tmp_path / "yett-vpn-forced"
    temp_dir.mkdir()
    monkeypatch.setattr(vpn_module.tempfile, "mkdtemp", lambda **kwargs: str(temp_dir))
    monkeypatch.setattr(vpn_module.os, "write", lambda fd, data: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        _write_secret_temp(b"secret", suffix=".auth")
    assert not temp_dir.exists()


def test_output_pump_caps_then_zeroes_sensitive_capture() -> None:
    fake = _FakeProc()
    owned = OwnedProcess(pid=fake.pid, argv0="openvpn", _proc=fake)
    vpn_module._pump(io.BytesIO(b"secret=" + b"x" * 100_000), owned._stdout_buf, owned)
    assert len(owned._stdout_buf) == 64_000
    owned.suppress_output()
    assert owned._stdout_buf == bytearray()


def test_subprocess_output_drains_across_distinct_event_loops_without_deadlock() -> None:
    commander = vpn_module.SubprocessArgvCommander()
    script = "import sys; sys.stdout.buffer.write(b'x'*100000); sys.stdout.flush()"
    owned = asyncio.run(commander.start([sys.executable, "-c", script]))
    assert asyncio.run(owned.wait(timeout=5)) == 0
    asyncio.run(owned.drain_output())
    assert len(owned._stdout_buf) == 0


def test_pid_identity_mismatch_prevents_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeProc()
    owned = OwnedProcess(pid=fake.pid, argv0="openvpn", _proc=fake, _identity="original")
    monkeypatch.setattr(vpn_module, "_process_identity", lambda pid: "reused")
    owned.terminate()
    assert fake.returncode is None


def test_owned_process_group_signal_requires_matching_leader_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeProc()
    owned = OwnedProcess(
        pid=fake.pid,
        argv0="openfortivpn",
        _proc=fake,
        _identity="original",
        _owns_process_group=True,
    )
    sent: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(vpn_module, "_process_identity", lambda pid: "original")
    monkeypatch.setattr(vpn_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(vpn_module.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    owned.terminate_group()
    assert sent == [(fake.pid, vpn_module._TERMINATE_SIGNAL)]
    monkeypatch.setattr(vpn_module, "_process_identity", lambda pid: "reused")
    owned.kill_group()
    assert sent == [(fake.pid, vpn_module._TERMINATE_SIGNAL)]


async def test_invalid_binary_path_is_rejected_before_spawn() -> None:
    cmd = _FakeCommander()
    runner = SubprocessVpnRunner(
        {"office": _forti()},
        commander=cmd,
        binary_paths={"openfortivpn": "/tmp/not-the-approved-name"},
    )
    with pytest.raises(UserFacingError, match="binary"):
        await runner.connect("office", creds={"username": "a", "password": "p"})
    assert not cmd.calls


async def test_windows_native_commander_fails_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vpn_module.os, "name", "nt")
    with pytest.raises(UserFacingError, match="Windows native"):
        await vpn_module.SubprocessArgvCommander().start(["/usr/sbin/openvpn"])


@pytest.mark.parametrize("password", ["p@ss word", "semi;colon", "quote'\"value", "token=abc"])
async def test_secret_variants_never_appear_in_failure_result(password: str) -> None:
    profiles = {"office": _forti()}
    manager = VpnManager(
        SubprocessVpnRunner(profiles, commander=_FakeCommander(mode="fail_exit")),
        InMemorySecretStore({"vpn_pw": password}),
        profiles,
    )
    result = await VpnTool(manager).run(
        {"action": "connect", "profile": "office"},
        _Ctx(),
    )
    assert result.is_error
    assert password not in result.content


async def test_dynamic_secret_is_scrubbed_from_injected_commander_error() -> None:
    class LeakyCommander(_FakeCommander):
        async def start(self, argv, *, stdin=None, env=None):
            raise UserFacingError("backend accidentally echoed p@ss word")

    runner = SubprocessVpnRunner({"office": _forti()}, commander=LeakyCommander())
    with pytest.raises(UserFacingError) as exc:
        await runner.connect(
            "office",
            creds={"username": "a", "password": "p@ss word"},
        )
    assert "p@ss word" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


async def test_start_failure_removes_every_created_temp_file(tmp_path: Path) -> None:
    ovpn = tmp_path / "client.ovpn"
    ovpn.write_text("client\n", encoding="utf-8")
    cmd = _FakeCommander(mode="missing_bin")
    runner = SubprocessVpnRunner({"home": _ovpn(str(ovpn))}, commander=cmd)
    with pytest.raises(UserFacingError):
        await runner.connect("home", creds={"username": "a", "password": "p"})
    argv = cmd.calls[0]
    for option in ("--config", "--auth-user-pass"):
        assert not Path(argv[argv.index(option) + 1]).exists()


async def test_close_terminates_only_owned_ready_process() -> None:
    cmd = _FakeCommander()
    runner = SubprocessVpnRunner({"office": _forti()}, commander=cmd)
    assert await runner.connect("office", creds={"username": "a", "password": "p"})
    runner.close()
    assert cmd.started[0].returncode() == -15
    assert not runner._sessions


async def test_ssh_preconnect_propagates_cancel_token() -> None:
    from yett.tools.remote.hostprofile import HostProfile, HostRegistry
    from yett.tools.remote.ssh_exec import SshExecTool

    class CaptureVpn:
        seen: CancelToken | None = None

        async def ensure(self, profile, *, cancel=None):
            self.seen = cancel

    class Ssh:
        async def run(self, host, cmd, *, key):
            return (0, "ok", "")

    class Ctx:
        cancel = CancelToken()

    vpn = CaptureVpn()
    hosts = HostRegistry(
        {
            "uat": HostProfile(
                address="10.0.0.1",
                auth="keyfile:k",
                vpn_required="office",
            )
        }
    )
    tool = SshExecTool(hosts, Ssh(), InMemorySecretStore({"k": "KEY"}), vpn=vpn)
    result = await tool.run({"host": "uat", "cmd": "true"}, Ctx())
    assert result.ok
    assert vpn.seen is Ctx.cancel


async def test_ssh_preconnect_cannot_run_when_approval_is_denied() -> None:
    from yett.tools.registry import Registry
    from yett.tools.remote.hostprofile import HostProfile, HostRegistry
    from yett.tools.remote.ssh_exec import SshExecTool
    from yett.tools.wiring import execute_tool

    class CaptureVpn:
        calls = 0

        async def ensure(self, profile, *, cancel=None):
            self.calls += 1

    class CaptureSsh:
        calls = 0

        async def run(self, host, cmd, *, key):
            self.calls += 1
            return (0, "ok", "")

    async def deny(_tool: str, _args: dict) -> bool:
        return False

    hosts = HostRegistry(
        {
            "uat": HostProfile(
                address="10.0.0.1",
                auth="keyfile:k",
                vpn_required="office",
            )
        }
    )
    vpn = CaptureVpn()
    ssh = CaptureSsh()
    registry = Registry()
    registry.register(
        SshExecTool(hosts, ssh, InMemorySecretStore({"k": "KEY"}), vpn=vpn)
    )
    result = await execute_tool(
        "ssh_exec",
        {"host": "uat", "cmd": "tail -n 10 /var/log/app/x.log"},
        _Ctx(),
        gate=BasicGate(SecurityCfg(), hosts=hosts),
        registry=registry,
        approver=deny,
    )
    assert result.is_error and "approval" in result.content
    assert vpn.calls == 0
    assert ssh.calls == 0


def test_openvpn_config_requires_ovpn_suffix(tmp_path: Path) -> None:
    config = tmp_path / "client.conf"
    config.write_text("client\n", encoding="utf-8")
    with pytest.raises(Exception):
        VpnProfileCfg(kind="openvpn", config_file=str(config))
