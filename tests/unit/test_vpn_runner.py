"""Adversarial / offline tests for SubprocessVpnRunner + VPN wiring.

Không gọi openfortivpn/openvpn thật — commander injectable. Bảo đảm: argv-only, allowlist,
không secret trong lỗi/result, cleanup temp, ownership disconnect, timeout, missing binary,
injection/traversal/symlink, duplicate connect, redaction.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from yett.config.models import VpnProfileCfg
from yett.core.cancel import CancelToken, Cancelled
from yett.errors import UserFacingError
from yett.secrets.backends import InMemorySecretStore
from yett.config.models import SecurityCfg
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
    assert "--config" in argv and str(ovpn) in argv
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
    assert d3.verdict == "allow"


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
