"""Hooks (spec P2 §5). PreToolUse/PostToolUse với quyền mutate/deny.

Thứ tự trong wiring: Gate → PreToolUse → validate → run → PostToolUse → Filters.
Hook KHÔNG nới được quyết định deny của Gate (Gate chạy trước). enforcing lỗi → deny;
non-enforcing lỗi → bỏ qua + log. Hook có timeout riêng, lỗi không giết agent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Literal, Protocol


@dataclass
class HookEvent:
    name: str  # "PreToolUse" | "PostToolUse" | ...
    tool: str
    args: dict
    result_content: str | None = None


@dataclass
class HookOutcome:
    action: Literal["continue", "mutate", "deny"] = "continue"
    mutated_args: dict | None = None
    mutated_result: str | None = None
    reason: str = ""


class Hook(Protocol):
    events: list[str]
    enforcing: bool
    timeout_sec: float

    async def handle(self, event: HookEvent) -> HookOutcome: ...


class HookRunner:
    def __init__(self, hooks: list[Hook], *, logger: Callable[..., None] | None = None) -> None:
        self._hooks = hooks
        self._log = logger or (lambda **k: None)

    async def run(self, event: HookEvent) -> HookOutcome:
        """Chạy các hook đăng ký event này theo thứ tự. deny dừng ngay; mutate tích luỹ."""
        args = dict(event.args)
        result = event.result_content
        for hook in self._hooks:
            if event.name not in hook.events:
                continue
            ev = HookEvent(event.name, event.tool, args, result)
            try:
                outcome = await asyncio.wait_for(hook.handle(ev), timeout=hook.timeout_sec)
            except Exception as e:  # noqa: BLE001
                self._log(event="hook_error", hook=type(hook).__name__, err=str(e))
                if hook.enforcing:
                    return HookOutcome("deny", reason=f"enforcing hook lỗi: {e}")
                continue  # non-enforcing: bỏ qua, agent tiếp tục
            if outcome.action == "deny":
                return outcome
            if outcome.action == "mutate":
                if outcome.mutated_args is not None:
                    args = outcome.mutated_args
                if outcome.mutated_result is not None:
                    result = outcome.mutated_result
        return HookOutcome("mutate", mutated_args=args, mutated_result=result)
