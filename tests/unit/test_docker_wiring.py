"""Docker sandbox wiring (spec P0-P1 §3.7): mount computation, host→container cwd mapping,
fail-closed khi thiếu Docker, hardening flags trên argv sinh ra.

Không cần daemon Docker chạy thật: test chỉ assert trên argv sinh ra (`_base_flags`) và trên
hành vi fail-closed khi mock `docker_unavailable_reason`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yett.app import App, _cwd_mapper, _project_roots
from yett.config.models import HarnessCfg, ProjectCfg, ProviderCfg, SandboxCfg
from yett.doctor import _docker_config_problem, run_doctor
from yett.errors import UserFacingError
from yett.provider.fake import FakeProvider
from yett.sandbox.base import ExecResult
from yett.sandbox.docker import DockerSandbox, probe_docker
from yett.tools.builtin.exec import ExecTool
from yett.tools.projects import ProjectScope


class _Ctx:
    session_key = "t"


class _RecordingSandbox:
    """Sandbox giả ghi lại (cmd, cwd) được gọi — dùng để test ExecTool mà không đụng subprocess."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(self, cmd, *, cwd=None, timeout=120, env=None) -> ExecResult:
        self.calls.append((cmd, cwd))
        return ExecResult("ok", "", 0)

    async def cleanup(self) -> None:
        return None


def _cfg(tmp_path: Path, *, backend: str = "docker", projects: dict | None = None) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend=backend),
        projects=projects or {},
    )


# ---------------------------------------------------------------------------
# Mount computation (_project_roots)
# ---------------------------------------------------------------------------

def test_project_roots_maps_workspace_to_container_root(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    cfg = _cfg(tmp_path)
    roots = _project_roots(cfg)
    assert roots[(tmp_path / "ws").resolve()] == "/workspace"


def test_project_roots_mounts_external_project_separately(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "proj").mkdir()
    cfg = _cfg(tmp_path, projects={"api": ProjectCfg(path=tmp_path / "proj")})
    roots = _project_roots(cfg)
    assert roots[(tmp_path / "ws").resolve()] == "/workspace"
    assert roots[(tmp_path / "proj").resolve()] == "/workspace/projects/api"


def test_project_roots_skips_project_nested_in_workspace(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "sub").mkdir()
    cfg = _cfg(tmp_path, projects={"sub": ProjectCfg(path=tmp_path / "ws" / "sub")})
    roots = _project_roots(cfg)
    # project đã nằm trong workspace_root → không mount trùng, chỉ có 1 root
    assert list(roots.values()) == ["/workspace"]


# ---------------------------------------------------------------------------
# host -> container cwd mapping (_cwd_mapper)
# ---------------------------------------------------------------------------

def test_cwd_mapper_local_backend_is_identity(tmp_path: Path) -> None:
    mapper = _cwd_mapper("local", {})
    p = tmp_path / "anything"
    assert mapper(p) == str(p)


def test_cwd_mapper_docker_maps_workspace_root(tmp_path: Path) -> None:
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    mapper = _cwd_mapper("docker", {ws: "/workspace"})
    assert mapper(ws) == "/workspace"


def test_cwd_mapper_docker_maps_nested_path(tmp_path: Path) -> None:
    ws = (tmp_path / "ws").resolve()
    (ws / "src").mkdir(parents=True)
    mapper = _cwd_mapper("docker", {ws: "/workspace"})
    assert mapper(ws / "src") == "/workspace/src"


def test_cwd_mapper_docker_picks_longest_matching_root(tmp_path: Path) -> None:
    ws = (tmp_path / "ws").resolve()
    proj = (tmp_path / "ws" / "proj").resolve()
    proj.mkdir(parents=True)
    mapper = _cwd_mapper("docker", {ws: "/workspace", proj: "/workspace/projects/p"})
    # path nằm trong 'proj' phải khớp root proj (dài hơn), không phải root ws
    assert mapper(proj / "file") == "/workspace/projects/p/file"


def test_cwd_mapper_docker_raises_when_no_root_matches(tmp_path: Path) -> None:
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    other = (tmp_path / "outside").resolve()
    other.mkdir()
    mapper = _cwd_mapper("docker", {ws: "/workspace"})
    with pytest.raises(UserFacingError):
        mapper(other)


# ---------------------------------------------------------------------------
# ExecTool: scope-check host cwd trước, rồi map sang sandbox path
# ---------------------------------------------------------------------------

async def test_exec_tool_maps_in_scope_cwd_to_container_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    scope = ProjectScope(ws, {})
    sandbox = _RecordingSandbox()
    tool = ExecTool(sandbox, scope=scope, to_sandbox_path=_cwd_mapper("docker", {ws.resolve(): "/workspace"}))
    res = await tool.run({"cmd": "echo hi", "cwd": str(ws / "sub")}, _Ctx())
    assert not res.is_error
    assert sandbox.calls[0][1] == "/workspace/sub"


async def test_exec_tool_denies_out_of_scope_cwd(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = ProjectScope(ws, {})
    sandbox = _RecordingSandbox()
    tool = ExecTool(sandbox, scope=scope, to_sandbox_path=_cwd_mapper("docker", {ws.resolve(): "/workspace"}))
    with pytest.raises(UserFacingError):
        await tool.run({"cmd": "echo hi", "cwd": str(outside)}, _Ctx())
    assert sandbox.calls == []  # scope-check chặn trước khi chạm sandbox


async def test_exec_tool_without_scope_keeps_backward_compat(tmp_path: Path) -> None:
    """Không truyền scope/mapper (test cũ dùng ExecTool(LocalSandbox())) → hành vi cũ giữ nguyên."""
    sandbox = _RecordingSandbox()
    tool = ExecTool(sandbox)
    res = await tool.run({"cmd": "echo hi", "cwd": "/some/raw/path"}, _Ctx())
    assert not res.is_error
    assert sandbox.calls[0][1] == "/some/raw/path"


# ---------------------------------------------------------------------------
# Fail-closed: backend=docker + Docker/daemon vắng → UserFacingError rõ ràng
# ---------------------------------------------------------------------------

def test_probe_docker_raises_when_cli_missing(monkeypatch) -> None:
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: None)
    with pytest.raises(UserFacingError, match="docker"):
        probe_docker()


def test_probe_docker_raises_when_daemon_unreachable(monkeypatch) -> None:
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: "/usr/bin/docker")

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "error during connect: daemon vắng"

    monkeypatch.setattr("yett.sandbox.docker.subprocess.run", lambda *a, **k: _FakeResult())
    with pytest.raises(UserFacingError, match="daemon"):
        probe_docker()


def test_probe_docker_ok_when_daemon_reachable(monkeypatch) -> None:
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: "/usr/bin/docker")

    class _FakeResult:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("yett.sandbox.docker.subprocess.run", lambda *a, **k: _FakeResult())
    probe_docker()  # không raise


def test_app_fails_closed_when_backend_docker_and_docker_missing(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: None)
    cfg = _cfg(tmp_path, backend="docker")
    with pytest.raises(UserFacingError):
        App(cfg, FakeProvider([]), state_dir=tmp_path / "state")


def test_app_does_not_fall_back_to_local_sandbox_on_docker_missing(tmp_path: Path, monkeypatch) -> None:
    """Trước fix: App._init_ dùng `sandbox or LocalSandbox()` → docker vắng vẫn chạy host.
    Sau fix: phải raise, KHÔNG bao giờ tạo ra App với LocalSandbox âm thầm."""
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: None)
    cfg = _cfg(tmp_path, backend="docker")
    try:
        App(cfg, FakeProvider([]), state_dir=tmp_path / "state")
    except UserFacingError as e:
        assert "docker" in str(e).lower()
    else:
        pytest.fail("App phải fail-closed khi backend=docker và Docker vắng, không được khởi tạo im lặng")


def test_app_builds_docker_sandbox_with_mounts_when_docker_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: "/usr/bin/docker")

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("yett.sandbox.docker.subprocess.run", lambda *a, **k: _FakeResult())
    (tmp_path / "ws").mkdir()
    cfg = _cfg(tmp_path, backend="docker")
    app = App(cfg, FakeProvider([]), state_dir=tmp_path / "state")
    exec_tool = app.registry.get("exec")
    assert isinstance(exec_tool._sandbox, DockerSandbox)
    assert exec_tool._sandbox._mounts == {str((tmp_path / "ws").resolve()): "/workspace"}
    app.close()


# ---------------------------------------------------------------------------
# DockerSandbox: đủ hardening flags docstring hứa; không còn ternary no-op --network
# ---------------------------------------------------------------------------

def test_docker_sandbox_base_flags_include_all_hardening() -> None:
    cfg = SandboxCfg(backend="docker", network="none", mem_limit="1g", cpus=1.5)
    sb = DockerSandbox(cfg, mounts={"/host/ws": "/workspace"})
    flags = sb._base_flags()
    assert "--rm" in flags
    assert flags[flags.index("--network") + 1] == "none"
    assert flags[flags.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in flags and "no-new-privileges" in flags
    assert "--read-only" in flags
    assert flags[flags.index("--tmpfs") + 1].startswith("/tmp:")
    # --user: non-root luôn luôn; giá trị cụ thể phụ thuộc host (test riêng bên dưới)
    user_val = flags[flags.index("--user") + 1]
    assert user_val and not user_val.startswith("0:"), f"container không được chạy root: {user_val}"
    assert flags[flags.index("--pids-limit") + 1] == "128"
    assert flags[flags.index("--memory") + 1] == "1g"
    assert flags[flags.index("--cpus") + 1] == "1.5"
    assert "-v" in flags
    v_idx = flags.index("-v")
    assert flags[v_idx + 1] == "/host/ws:/workspace:rw"


def test_docker_sandbox_user_maps_host_uid_gid_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind mount :rw giữ nguyên quyền host trên Linux/macOS — container phải chạy đúng
    UID/GID host, nếu không (65534 nobody) exec KHÔNG ghi được workspace do user sở hữu."""
    import yett.sandbox.docker as docker_mod

    monkeypatch.setattr(docker_mod.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(docker_mod.os, "getgid", lambda: 1000, raising=False)
    flags = DockerSandbox(SandboxCfg(backend="docker"))._base_flags()
    assert flags[flags.index("--user") + 1] == "1000:1000"


def test_docker_sandbox_user_falls_back_to_nobody_for_root_and_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host chạy root (uid 0) → ép 65534 giữ lời hứa non-root; Windows host (không có
    os.getuid) → 65534 (Docker Desktop tự map quyền file-sharing)."""
    import yett.sandbox.docker as docker_mod

    monkeypatch.setattr(docker_mod.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(docker_mod.os, "getgid", lambda: 0, raising=False)
    flags_root = DockerSandbox(SandboxCfg(backend="docker"))._base_flags()
    assert flags_root[flags_root.index("--user") + 1] == "65534:65534"

    monkeypatch.delattr(docker_mod.os, "getuid", raising=False)
    monkeypatch.delattr(docker_mod.os, "getgid", raising=False)
    flags_win = DockerSandbox(SandboxCfg(backend="docker"))._base_flags()
    assert flags_win[flags_win.index("--user") + 1] == "65534:65534"


def test_docker_sandbox_network_reflects_cfg_not_hardcoded_ternary() -> None:
    """`--network` phải phản ánh đúng cfg.network (bug cũ: ternary no-op luôn == cfg.network
    nhưng khó đọc/nhầm là ép cứng "none"). Test cả 2 giá trị hợp lệ."""
    cfg_none = SandboxCfg(backend="docker", network="none")
    cfg_proxy = SandboxCfg(backend="docker", network="proxy")
    flags_none = DockerSandbox(cfg_none)._base_flags()
    flags_proxy = DockerSandbox(cfg_proxy)._base_flags()
    assert flags_none[flags_none.index("--network") + 1] == "none"
    assert flags_proxy[flags_proxy.index("--network") + 1] == "proxy"


async def test_docker_sandbox_run_passes_cwd_as_workdir_flag() -> None:
    """`run()` phải phát `-w <cwd>` với cwd đã map sẵn (không tự suy path)."""
    calls: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            return None

        async def wait(self):
            return None

    async def _fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeProc()

    import asyncio as _asyncio

    orig = _asyncio.create_subprocess_exec
    _asyncio.create_subprocess_exec = _fake_exec  # type: ignore[assignment]
    try:
        sb = DockerSandbox(SandboxCfg(backend="docker"))
        await sb.run(["echo", "hi"], cwd="/workspace/sub", timeout=5)
    finally:
        _asyncio.create_subprocess_exec = orig  # type: ignore[assignment]
    argv = calls[0]
    assert argv[argv.index("-w") + 1] == "/workspace/sub"


# ---------------------------------------------------------------------------
# doctor.py: báo rõ khi config khai backend=docker mà Docker chưa sẵn sàng
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, backend: str) -> Path:
    p = tmp_path / "harness.yaml"
    p.write_text(
        f"provider:\n  name: fake\n  model: fake-1\n"
        f"workspace_root: {tmp_path / 'ws'}\nsandbox:\n  backend: {backend}\n",
        encoding="utf-8",
    )
    return p


def test_docker_config_problem_none_when_config_missing(tmp_path: Path) -> None:
    assert _docker_config_problem(tmp_path / "no-such-config.yaml") is None


def test_docker_config_problem_none_when_backend_local(tmp_path: Path) -> None:
    p = _write_config(tmp_path, "local")
    assert _docker_config_problem(p) is None


def test_docker_config_problem_reports_reason_when_docker_missing(tmp_path: Path, monkeypatch) -> None:
    p = _write_config(tmp_path, "docker")
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: None)
    problem = _docker_config_problem(p)
    assert problem is not None and "docker" in problem.lower()


def test_docker_config_problem_none_when_docker_ready(tmp_path: Path, monkeypatch) -> None:
    p = _write_config(tmp_path, "docker")
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: "/usr/bin/docker")

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("yett.sandbox.docker.subprocess.run", lambda *a, **k: _FakeResult())
    assert _docker_config_problem(p) is None


def test_run_doctor_flags_docker_backend_without_docker(tmp_path: Path, monkeypatch) -> None:
    p = _write_config(tmp_path, "docker")
    monkeypatch.setattr("yett.sandbox.docker.shutil.which", lambda _: None)
    lines: list[str] = []
    rc = run_doctor(emit=lines.append, config_path=p)
    joined = "\n".join(lines)
    assert "TỪ CHỐI" in joined
    assert rc >= 1


def test_run_doctor_unaffected_when_no_config_present(tmp_path: Path) -> None:
    """Không có config tại config_path (mặc định) → hành vi giữ nguyên như trước (backward
    compat với test_doctor_configio.py::test_doctor_runs_and_reports)."""
    lines: list[str] = []
    rc = run_doctor(emit=lines.append, config_path=tmp_path / "absent.yaml")
    assert rc == 0
    assert "TỪ CHỐI" not in "\n".join(lines)
