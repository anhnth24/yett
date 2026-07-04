"""DockerSandbox — chạy lệnh trong container hardened (spec P0-P1 §3.7).

Bản v0.1: bọc `docker run` qua CLI với hardening flags. (P0.2: thay bằng vendored
Hermes DockerEnvironment để tái dùng session lifecycle; interface Sandbox giữ nguyên.)

Hardening: --network none (mặc định), --cap-drop ALL, --security-opt no-new-privileges,
--read-only rootfs (trừ mount project), memory/cpu limit, --rm.
"""

from __future__ import annotations

import asyncio

from yett.config.models import SandboxCfg
from yett.sandbox.base import ExecResult


class DockerSandbox:
    def __init__(self, cfg: SandboxCfg, image: str = "python:3.11-slim", mounts: dict[str, str] | None = None) -> None:
        self._cfg = cfg
        self._image = image
        self._mounts = mounts or {}  # host_path -> container_path (read-write project mounts)

    def _base_flags(self, timeout: int) -> list[str]:
        flags = [
            "docker", "run", "--rm",
            "--network", self._cfg.network if self._cfg.network != "none" else "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", self._cfg.mem_limit,
            "--cpus", str(self._cfg.cpus),
        ]
        for host, cont in self._mounts.items():
            flags += ["-v", f"{host}:{cont}:rw"]
        return flags

    async def run(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int | None = None, env: dict | None = None
    ) -> ExecResult:
        timeout = timeout or self._cfg.timeout_sec
        flags = self._base_flags(timeout)
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
