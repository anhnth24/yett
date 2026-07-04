"""Channel gating (spec P3 §4). Pairing code + allowlist chat_id.

Chỉ người được ghép mới sai khiến agent. Channel là entry adapter TRƯỚC Session Manager —
cùng core, cùng Policy Gate; gating chỉ quyết định AI được phép gửi message vào.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelGate:
    allowed_chat_ids: set[str] = field(default_factory=set)
    _pending_codes: dict[str, str] = field(default_factory=dict)  # code -> chat_id

    def is_allowed(self, chat_id: str) -> bool:
        return chat_id in self.allowed_chat_ids

    def request_pairing(self, chat_id: str, code: str) -> None:
        """Người dùng gửi code để ghép; lưu chờ approve từ owner."""
        self._pending_codes[code] = chat_id

    def approve_pairing(self, code: str) -> str | None:
        """Owner duyệt code → chat_id vào allowlist. Trả chat_id hoặc None nếu code sai."""
        chat_id = self._pending_codes.pop(code, None)
        if chat_id is None:
            return None
        self.allowed_chat_ids.add(chat_id)
        return chat_id

    def revoke(self, chat_id: str) -> None:
        self.allowed_chat_ids.discard(chat_id)
