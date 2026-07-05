"""Docker sandbox integration (spec P0-P1 §3.7).

Yêu cầu Docker daemon chạy thật (`docker info` phải trả về 0) — skip sạch nếu không có
(máy dev không cài Docker, hoặc CI runner không có daemon). Chứng minh bằng hành vi thật:

1. workspace được MOUNT + exec chạy THẬT trong container (không phải trên host) — kiểm bằng
   dấu hiệu chỉ có trong container Linux (`/etc/os-release`) và bằng việc đọc lại file đã ghi
   từ phía host qua đường mount.
2. KHÔNG egress — `--network none` chặn cả DNS/HTTP.
3. `cwd` host hợp lệ được map đúng sang path container; `cwd` ngoài scope bị ExecTool chặn
   TRƯỚC khi chạm sandbox (không có container nào được khởi động).

Image `python:3.11-slim` phải có sẵn hoặc kéo được (`docker pull`) — nếu máy chạy test không
có mạng để pull, test sẽ fail rõ ràng ở bước exec (không skip nhầm thành "an toàn giả").
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from yett.app import App
from yett.config.models import HarnessCfg, ProviderCfg, SandboxCfg
from yett.errors import UserFacingError
from yett.provider.fake import FakeProvider


def _docker_daemon_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_daemon_ready(), reason="cần Docker daemon chạy thật ('docker info' phải OK)"
)


class _Ctx:
    session_key = "t"


def _cfg(tmp_path: Path) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="docker", timeout_sec=60),
    )


async def test_exec_runs_in_container_with_workspace_mounted(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "marker.txt").write_text("ghi từ host", encoding="utf-8")
    app = App(provider=FakeProvider([]), cfg=_cfg(tmp_path), state_dir=tmp_path / "state")
    try:
        exec_tool = app.registry.get("exec")
        # dấu hiệu chỉ có trong container Linux (Debian slim) — chứng minh KHÔNG chạy trên host
        res = await exec_tool.run({"cmd": "cat /etc/os-release"}, _Ctx())
        assert not res.is_error
        assert "debian" in res.content.lower()
        # workspace mount: file ghi từ host phải đọc được trong container qua path đã map
        res2 = await exec_tool.run(
            {"cmd": "cat marker.txt", "cwd": str(tmp_path / "ws")}, _Ctx()
        )
        assert not res2.is_error
        assert "ghi từ host" in res2.content
    finally:
        app.close()


async def test_exec_cwd_mapped_to_container_workspace_path(tmp_path: Path) -> None:
    (tmp_path / "ws" / "sub").mkdir(parents=True)
    app = App(provider=FakeProvider([]), cfg=_cfg(tmp_path), state_dir=tmp_path / "state")
    try:
        exec_tool = app.registry.get("exec")
        res = await exec_tool.run({"cmd": "pwd", "cwd": str(tmp_path / "ws" / "sub")}, _Ctx())
        assert not res.is_error
        assert "/workspace/sub" in res.content
    finally:
        app.close()


async def test_exec_has_no_network_egress(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    app = App(provider=FakeProvider([]), cfg=_cfg(tmp_path), state_dir=tmp_path / "state")
    try:
        exec_tool = app.registry.get("exec")
        res = await exec_tool.run(
            {"cmd": "python3 -c \"import urllib.request as u; u.urlopen('http://example.com', timeout=3)\""},
            _Ctx(),
        )
        assert res.is_error  # --network none → không resolve/connect được
    finally:
        app.close()


async def test_exec_denies_cwd_outside_scope_before_touching_sandbox(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "outside").mkdir()
    app = App(provider=FakeProvider([]), cfg=_cfg(tmp_path), state_dir=tmp_path / "state")
    try:
        exec_tool = app.registry.get("exec")
        with pytest.raises(UserFacingError):
            await exec_tool.run({"cmd": "echo hi", "cwd": str(tmp_path / "outside")}, _Ctx())
    finally:
        app.close()
