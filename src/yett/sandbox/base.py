"""Sandbox interface + kết quả (spec P0-P1 §3.5).

Bọc backend thực thi (local dev, docker prod, ssh remote ở P2). Adapter áp SandboxCfg
(network=none, timeout, limits). Vendored Hermes environments sẽ implement Protocol này.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


class Sandbox(Protocol):
    async def run(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int = 120, env: dict | None = None
    ) -> ExecResult: ...

    async def cleanup(self) -> None: ...
