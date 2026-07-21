"""DockerSandbox — chạy lệnh trong container hardened (spec P0-P1 §3.7).

Bọc `docker run` qua CLI với hardening flags CỨNG (không cấu hình thêm ngoài SandboxCfg):
`--network` (mặc định "none" — không egress), `--cap-drop ALL`, `--security-opt
no-new-privileges`, `--read-only` rootfs + `--tmpfs /tmp` cho scratch (ghi tạm vì rootfs
read-only), `--user` non-root (POSIX: UID/GID host thật để ghi được bind mount :rw;
Windows host: 65534 nobody — Docker Desktop tự map quyền file-sharing), `--pids-limit`,
`--memory`/`--cpus` theo SandboxCfg, `--rm`. Timeout xử lý ở tầng orchestration
(asyncio.wait_for + kill tiến trình khi quá hạn), không phải flag `docker run`.

Mount workspace/project (rw) do App tính & truyền vào qua `mounts` (host path -> container
path). `-w` (cwd) nhận thẳng path container đã map sẵn ở phía gọi (xem `app.py`); DockerSandbox
không tự suy path — chỉ phát flag.

(P0.2 tương lai: thay bằng vendored Hermes DockerEnvironment để tái dùng session lifecycle;
interface `Sandbox` giữ nguyên.)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from yett.config.models import SandboxCfg
from yett.errors import UserFacingError
from yett.sandbox.base import ExecResult

_DOCKER_PROBE_TIMEOUT_SEC = 5


def docker_unavailable_reason() -> str | None:
    """Trả `None` nếu Docker CLI + daemon sẵn sàng; ngược lại trả lý do (tiếng Việt, ngắn)."""
    if shutil.which("docker") is None:
        return "không tìm thấy lệnh 'docker' trong PATH"
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=_DOCKER_PROBE_TIMEOUT_SEC, text=True
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"gọi 'docker info' lỗi/timeout ({e})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return "daemon chưa chạy ('docker info' thất bại" + (f": {detail[-1]}" if detail else "") + ")"
    return None


def probe_docker() -> None:
    """Fail-closed: raise `UserFacingError` rõ nếu Docker CLI/daemon chưa sẵn sàng.

    Gọi trước khi dựng `DockerSandbox` (App) — KHÔNG bao giờ hạ cấp âm thầm về LocalSandbox."""
    reason = docker_unavailable_reason()
    if reason:
        raise UserFacingError(
            f"sandbox.backend='docker' nhưng Docker chưa sẵn sàng ({reason}). "
            "Cài/mở Docker Desktop (Windows/macOS) hoặc khởi động dịch vụ docker (Linux) rồi "
            "thử lại; hoặc đổi tạm sang sandbox.backend='local' (CHỈ dev/test — KHÔNG cô lập) "
            "trong config. Chạy 'yett doctor' để xem chi tiết cài đặt."
        )


def _container_user() -> str:
    """UID:GID chạy trong container. Linux/macOS native: bind mount :rw giữ nguyên quyền
    host — 65534 (nobody) sẽ KHÔNG ghi được workspace do user host sở hữu, nên dùng đúng
    UID/GID host. Windows host không có os.getuid (Docker Desktop/WSL2 tự map quyền qua
    lớp file-sharing) → giữ 65534:65534 non-root. Host chạy bằng root (uid 0) → vẫn ép
    65534 để giữ lời hứa non-root trong container."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return "65534:65534"
    uid, gid = getuid(), getgid()
    if uid == 0:
        return "65534:65534"
    return f"{uid}:{gid}"


class DockerSandbox:
    def __init__(
        self,
        cfg: SandboxCfg,
        image: str = "python:3.11-slim",
        mounts: dict[str, str] | None = None,
        readonly_overlays: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._cfg = cfg
        self._image = image
        self._mounts = mounts or {}  # host_path -> container_path (read-write project mounts)
        # container path -> (preferred host path, safe fallback file).  Resolve at each run:
        # MEMORY.md may not exist when App starts, then appear after an operator approval.
        self._readonly_overlays = readonly_overlays or {}

    def _base_flags(self) -> list[str]:
        flags = [
            "docker", "run", "--rm",
            "--network", self._cfg.network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--user", _container_user(),
            "--pids-limit", "128",
            "--memory", self._cfg.mem_limit,
            "--cpus", str(self._cfg.cpus),
        ]
        for host, cont in self._mounts.items():
            flags += ["-v", f"{host}:{cont}:rw"]
        for cont, (preferred, fallback) in self._readonly_overlays.items():
            candidate = Path(preferred)
            # Never pass a symlink to Docker as a protected overlay.
            host = preferred if candidate.exists() and not candidate.is_symlink() else fallback
            flags += ["-v", f"{host}:{cont}:ro"]
        return flags

    async def run(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int | None = None, env: dict | None = None
    ) -> ExecResult:
        timeout = timeout or self._cfg.timeout_sec
        flags = self._base_flags()
        if cwd:
            flags += ["-w", cwd]
        for k, v in (env or {}).items():
            flags += ["-e", f"{k}={v}"]
        full = flags + [self._image] + cmd
        proc = await asyncio.create_subprocess_exec(
            *full, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult("", f"timeout sau {timeout}s", 124, timed_out=True)
        return ExecResult(
            out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
        )

    async def cleanup(self) -> None:
        return None
