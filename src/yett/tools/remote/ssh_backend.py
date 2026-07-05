"""SSH backend thật (spec P2 §2.3). Dùng asyncssh (lazy import — chỉ cần khi thực sự SSH).

key/credential truyền vào tại điểm dùng, KHÔNG log. Backend inject được → test offline dùng fake.
"""

from __future__ import annotations

from yett.tools.remote.hostprofile import HostProfile


class AsyncSSHBackend:
    async def run(self, host: HostProfile, cmd: str, *, key: str) -> tuple[int, str, str]:
        try:
            import asyncssh  # lazy
        except ImportError as e:
            raise RuntimeError("cần cài 'asyncssh' để dùng SSH remote: pip install asyncssh") from e

        # host.address có thể kèm user@ hoặc dùng field riêng; parse tối giản.
        addr = host.address
        user = None
        if "@" in addr:
            user, addr = addr.split("@", 1)
        conn_kwargs: dict = {"known_hosts": None}  # [Inference] production nên pin known_hosts
        if user:
            conn_kwargs["username"] = user
        if key:
            conn_kwargs["client_keys"] = [asyncssh.import_private_key(key)]
        async with asyncssh.connect(addr, **conn_kwargs) as conn:
            result = await conn.run(cmd, check=False)
            code = result.exit_status if result.exit_status is not None else 0
            return int(code), str(result.stdout or ""), str(result.stderr or "")
