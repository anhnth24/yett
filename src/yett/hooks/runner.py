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
        """Chạy các hook đăng ký event này theo thứ tự. deny dừng ngay; mutate tích luỹ.

        Chỉ trả action="mutate" khi CÓ hook thực sự đổi args/result — nếu không (kể cả khi
        danh sách hook rỗng) trả "continue" với mutated_*=None. Caller (wiring) dựa vào
        `mutated_args is not None` để quyết định re-gate/re-approve: nếu ở đây luôn trả args
        (dù không đổi), tool cần duyệt sẽ bị hỏi duyệt HAI lần."""
        args = dict(event.args)
        result = event.result_content
        args_mutated = False
        result_mutated = False
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
                    args_mutated = True
                if outcome.mutated_result is not None:
                    result = outcome.mutated_result
                    result_mutated = True
        if not args_mutated and not result_mutated:
            return HookOutcome("continue")
        return HookOutcome(
            "mutate",
            mutated_args=args if args_mutated else None,
            mutated_result=result if result_mutated else None,
        )
