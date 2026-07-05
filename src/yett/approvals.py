"""Approval center (spec P2.4.3 + web UI). Cầu nối approval giữa turn đang chạy và người dùng.

Khi Policy Gate trả need_approval, approver tạo một yêu cầu ở đây rồi CHỜ (có timeout).
Người dùng duyệt/từ chối qua kênh khác (web endpoint / terminal) → resolve() đánh thức.
Thread-safe: turn chạy trong thread riêng (asyncio.run), resolve gọi từ thread HTTP khác.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field


@dataclass
class _Pending:
    id: str
    tool: str
    args: dict
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class ApprovalCenter:
    def __init__(self, *, default_timeout: float = 300.0) -> None:
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._timeout = default_timeout

    def list_pending(self) -> list[dict]:
        with self._lock:
            return [{"id": p.id, "tool": p.tool, "args": _summarize(p.args)}
                    for p in self._pending.values()]

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """Người dùng quyết định. Trả False nếu id không tồn tại."""
        with self._lock:
            p = self._pending.get(approval_id)
            if p is None:
                return False
            p.approved = approved
            p.event.set()
            return True

    async def request(self, tool: str, args: dict) -> bool:
        """Approver: tạo yêu cầu + chờ resolve (timeout → deny sạch)."""
        p = _Pending(id=uuid.uuid4().hex[:8], tool=tool, args=dict(args))
        with self._lock:
            self._pending[p.id] = p
        try:
            # Chờ threading.Event trong executor để không chặn event loop của turn.
            ok = await asyncio.get_event_loop().run_in_executor(
                None, p.event.wait, self._timeout
            )
            return bool(ok and p.approved)
        finally:
            with self._lock:
                self._pending.pop(p.id, None)


def _summarize(args: dict) -> dict:
    """Rút gọn args để hiển thị (không lộ giá trị quá dài / secret đã redact ở tầng khác)."""
    out = {}
    for k, v in args.items():
        s = str(v)
        out[k] = s if len(s) <= 300 else s[:300] + "…"
    return out
