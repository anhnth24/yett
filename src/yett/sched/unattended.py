"""Approval trong run không giám sát (spec P2 P2.4.3).

Cron chạy lúc người dùng vắng → approval pending có timeout: hết hạn thì job FAIL sạch
+ audit lý do. KHÔNG treo vô hạn, KHÔNG auto-approve.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class TimeoutApprover:
    """Approver cho scheduled run: chờ tối đa timeout; hết hạn → False (deny sạch)."""

    def __init__(
        self,
        wait_fn: Callable[[str, dict], Awaitable[bool]],
        *,
        timeout_sec: float,
        auditor: Callable[..., None] | None = None,
    ) -> None:
        self._wait = wait_fn
        self._timeout = timeout_sec
        self._audit = auditor or (lambda **k: None)

    async def __call__(self, tool: str, args: dict) -> bool:
        try:
            approved = await asyncio.wait_for(self._wait(tool, args), timeout=self._timeout)
        except asyncio.TimeoutError:
            self._audit(event="approval_timeout", tool=tool)
            return False  # hết hạn → deny sạch
        self._audit(event="approval_result", tool=tool, approved=approved)
        return approved
