"""Context assembly + Prune (spec P0-P1 §4, P1.2.1/P1.2.3).

Deterministic: cùng input → cùng context (RG1-6). Token budget: ước lượng token đơn giản
(len/4) để không phụ thuộc tokenizer provider; Prune thay tool result cũ bằng tham chiếu.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yett.provider.base import Message


def estimate_tokens(text: str) -> int:
    """Ước lượng token ổn định (deterministic). ~4 ký tự/token."""
    return (len(text) + 3) // 4


@dataclass
class Context:
    system: str
    messages: list[Message] = field(default_factory=list)
    _tool_results: dict[str, str] = field(default_factory=dict)

    def add_user(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        self.messages.append(Message(role="assistant", content=text))

    def add_tool_result(self, call_id: str, content: str) -> None:
        self._tool_results[call_id] = content
        self.messages.append(Message(role="tool", content=content, tool_call_id=call_id))

    def tokens(self) -> int:
        total = estimate_tokens(self.system)
        for m in self.messages:
            total += estimate_tokens(m.content)
        return total

    def prune(self, budget: int) -> int:
        """Thay tool result cũ nhất bằng tham chiếu cho tới khi <= budget.
        Trả số message bị prune. Deterministic: luôn prune từ cũ đến mới."""
        pruned = 0
        for i, m in enumerate(self.messages):
            if self.tokens() <= budget:
                break
            if m.role == "tool" and not m.content.startswith("[pruned "):
                self.messages[i] = Message(
                    role="tool",
                    content=f"[pruned {len(m.content)} chars — tool result {m.tool_call_id}]",
                    tool_call_id=m.tool_call_id,
                )
                pruned += 1
        return pruned


def assemble_context(system: str, user_message: str, history: list[Message] | None = None) -> Context:
    """Ghép context ban đầu deterministic."""
    ctx = Context(system=system)
    for m in history or []:
        ctx.messages.append(m)
    ctx.add_user(user_message)
    return ctx
