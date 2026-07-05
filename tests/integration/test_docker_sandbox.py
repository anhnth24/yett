"""Docker sandbox isolation test (lộ RT-1/P0-1).

`build_app()`/`App.__init__` (src/yett/app.py:48,81) chỉ chọn `LocalSandbox` khi
`cfg.sandbox.backend == "local"`; mọi giá trị khác (kể cả "docker") trả `sandbox=None`,
rồi `App.__init__` fallback `sb = sandbox or LocalSandbox()`. Kết quả: `backend: docker`
KHÔNG BAO GIỜ thực sự dùng `DockerSandbox` — lệnh luôn chạy trực tiếp trên host, không
mount workspace, không cô lập mạng. Test này tái hiện đúng logic chọn sandbox đó (không
tạo App đầy đủ) rồi assert 3 tính chất mà chỉ Docker thật mới đảm bảo được.

Gate theo daemon: `skipif` sạch khi không có Docker CLI hoặc daemon không chạy — không có
nghĩa "pass giả". Khi CÓ daemon: `xfail(strict=True)` vì wiring hiện tại rơi về host
(Phase 3 sẽ wire DockerSandbox thật rồi gỡ marker).

Không gọi mạng thật để kiểm tra egress: dùng `socket.if_nameindex()` (syscall cục bộ, đếm
network interface) thay vì thử kết nối ra ngoài — `--network none` chỉ còn interface `lo`.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from yett.config.models import SandboxCfg
from yett.sandbox.base import Sandbox
from yett.sandbox.local import LocalSandbox
from yett.tools.builtin.exec import ExecTool


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_daemon_available(),
    reason="cần Docker daemon chạy sẵn (`docker info` phải thành công) — skip sạch khi thiếu",
)


class _HostCtx:
    session_key = "s1"


def _sandbox_for(cfg: SandboxCfg) -> Sandbox | None:
    """Tái hiện ĐÚNG logic wiring của build_app() (src/yett/app.py:48) — cố tình KHÔNG tạo
    DockerSandbox thật ở đây để bài test không phụ thuộc config loader/secret backend đầy
    đủ; chỉ cần tái hiện lỗi chọn sandbox."""
    return LocalSandbox() if cfg.backend == "local" else None


@pytest.mark.xfail(
    strict=True,
    reason="RT-1/P0-1: backend='docker' chưa wire DockerSandbox thật (app.py:48,81 fallback "
    "LocalSandbox chạy trên host) — không mount workspace, không cô lập mạng. Phase 3 fix "
    "rồi gỡ marker.",
)
async def test_docker_backend_isolates_network_and_mounts_workspace(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    marker = ws / "from-sandbox.txt"

    cfg = SandboxCfg(backend="docker", network="none")
    sandbox = _sandbox_for(cfg)
    # Mirror App.__init__ (app.py:81): sandbox=None (đúng như build_app trả về cho
    # backend != "local") fallback về LocalSandbox — đây chính là bug RT-1.
    sb = sandbox or LocalSandbox()
    exec_tool = ExecTool(sb, timeout=cfg.timeout_sec)
    ctx = _HostCtx()

    # (a) workspace phải được MOUNT + exec chạy được TRONG container: ghi file với cwd=ws
    # phải xuất hiện trở lại trên host qua mount (không phải vì exec chạy thẳng trên host).
    write_res = await exec_tool.run(
        {"cmd": f"echo containerized > {marker.name}", "cwd": str(ws)}, ctx
    )
    assert write_res.ok, write_res.content
    assert marker.exists(), "workspace không được mount vào container (RT-1)"

    # (b) không có network egress (--network none): container chỉ còn interface loopback.
    # Dùng socket.if_nameindex() (syscall cục bộ, không gửi gói tin) để không cần gọi
    # mạng thật trong test.
    iface_res = await exec_tool.run(
        {"cmd": 'python3 -c "import socket; print(len(socket.if_nameindex()))"'}, ctx
    )
    assert iface_res.ok, iface_res.content
    iface_count = int(iface_res.content.splitlines()[-1].strip() or "0")
    assert iface_count <= 1, (
        f"exec thấy {iface_count} network interface — container không cô lập mạng (RT-1); "
        "--network none chỉ nên còn loopback"
    )

    # (c) không chạy trên host: cgroup của chính tiến trình exec phải cho thấy đang ở
    # trong container (host CI runner trần không có "docker"/"containerd" trong cgroup).
    cgroup_res = await exec_tool.run({"cmd": "cat /proc/self/cgroup"}, ctx)
    assert cgroup_res.ok, cgroup_res.content
    assert "docker" in cgroup_res.content or "containerd" in cgroup_res.content, (
        "exec chạy trên host thay vì trong container (RT-1)"
    )
