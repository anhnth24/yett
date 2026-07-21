"""Context assembly + Prune (spec P0-P1 §4, P1.2.1/P1.2.3).

Deterministic: cùng input → cùng context (RG1-6). Token budget: ước lượng token đơn giản
(len/4) để không phụ thuộc tokenizer provider; Prune thay tool result cũ bằng tham chiếu.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from yett.provider.base import Message, ToolCall


def estimate_tokens(text: str) -> int:
    """Ước lượng token ổn định (deterministic). ~4 ký tự/token."""
    return (len(text) + 3) // 4


def tool_call_signature(name: str, args: dict) -> str:
    """Chữ ký ổn định cho một tool call (RT-2): dùng để nhận diện "tool call này đã chạy
    chưa" khi resume, KHÔNG dựa vào `id` do provider cấp (non-deterministic giữa các lần
    gọi model/khi resume). Deterministic: cùng name+args → cùng chữ ký."""
    payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return f"{name}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def pending_tool_calls(messages: list[Message]) -> list[ToolCall]:
    """Tool_call trong message assistant CUỐI CÙNG (nếu có) mà chưa có tool_result đứng
    sau — dùng khi resume để hoàn tất batch dang dở TRƯỚC khi hỏi model tiếp (RT-2/RT-3):
    gửi request còn tool_call chưa được trả lời là vi phạm thứ tự OpenAI/Anthropic."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role != "assistant":
            continue
        if not m.tool_calls:
            return []
        done_ids = {mm.tool_call_id for mm in messages[i + 1 :] if mm.role == "tool"}
        return [tc for tc in m.tool_calls if tc.id not in done_ids]
    return []


@dataclass
class Context:
    system: str
    messages: list[Message] = field(default_factory=list)
    _tool_results: dict[str, str] = field(default_factory=dict)

    def add_user(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        self.messages.append(Message(role="assistant", content=text))

    def add_assistant_tool_calls(self, tool_calls: list[ToolCall], text: str | None = None) -> None:
        """P0-2: ghi lượt assistant tool_use vào context TRƯỚC khi tool chạy — đúng thứ tự
        OpenAI/Anthropic (mọi message role=tool phải đứng ngay sau assistant mang tool_calls
        khớp id)."""
        self.messages.append(Message(role="assistant", content=text or "", tool_calls=list(tool_calls)))

    def add_tool_result(self, call_id: str, content: str, *, is_error: bool = False) -> None:
        self._tool_results[call_id] = content
        self.messages.append(
            Message(
                role="tool",
                content=content,
                tool_call_id=call_id,
                tool_result_is_error=is_error,
            )
        )

    def tokens(self) -> int:
        total = 0
        for m in self.messages:
            total += estimate_tokens(m.content)
            if m.tool_calls:
                # RT-12: assistant tool_use content thường rỗng nhưng args có thể nặng —
                # phải tính vào ước lượng, nếu không prune() nhắm sai chỗ và không hội tụ.
                for tc in m.tool_calls:
                    total += estimate_tokens(json.dumps(tc.args, ensure_ascii=False))
        return total

    def prune(self, budget: int) -> int:
        """Thay tool result cũ nhất bằng tham chiếu cho tới khi <= budget. Đồng thời rút gọn
        args của tool_call khớp trong message assistant tương ứng (RT-12) — loại cặp
        tool_use↔tool_result cùng nhau, giữ id để không phá cấu trúc hợp lệ. Nếu chỉ nhắm
        tool_result mà bỏ qua args (thường nặng hơn), prune có thể không hội tụ →
        ContextOverflow lặp tới hết max_iterations mà không giảm được token nào (livelock).
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
                    tool_result_is_error=m.tool_result_is_error,
                )
                self._prune_matching_tool_call(m.tool_call_id)
                pruned += 1
        return pruned

    def _prune_matching_tool_call(self, tool_call_id: str | None) -> None:
        """Rút gọn args của tool_call khớp `tool_call_id` trong message assistant chứa nó
        (giữ id/name — chỉ thay args nặng bằng placeholder) — nửa còn lại của cặp bị prune."""
        if tool_call_id is None:
            return
        for i, m in enumerate(self.messages):
            if m.role != "assistant" or not m.tool_calls:
                continue
            if not any(tc.id == tool_call_id and tc.args for tc in m.tool_calls):
                continue
            new_calls = [
                tc if tc.id != tool_call_id else ToolCall(id=tc.id, name=tc.name, args={"_pruned": True})
                for tc in m.tool_calls
            ]
            self.messages[i] = Message(
                role=m.role,
                content=m.content,
                tool_call_id=m.tool_call_id,
                tool_calls=new_calls,
                tool_result_is_error=m.tool_result_is_error,
            )
            return


def assemble_context(system: str, user_message: str, history: list[Message] | None = None) -> Context:
    """Ghép context ban đầu deterministic. P0-3: system prompt LUÔN là message đầu tiên gửi
    cho provider (trước đây chỉ nằm ở `Context.system`, không bao giờ tới model thật)."""
    ctx = Context(system=system)
    ctx.messages.append(Message(role="system", content=system))
    for m in history or []:
        ctx.messages.append(m)
    ctx.add_user(user_message)
    return ctx
