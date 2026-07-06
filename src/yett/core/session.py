"""Session Manager (spec P0-P1 §3.2). Map session_key → state; queue turn (không chạy song song)."""

from __future__ import annotations

from dataclasses import dataclass, field

from yett.provider.base import Message


@dataclass
class Session:
    key: str
    history: list[Message] = field(default_factory=list)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, key: str) -> Session:
        if key not in self._sessions:
            self._sessions[key] = Session(key=key)
        return self._sessions[key]

    def keys(self) -> list[str]:
        return sorted(self._sessions)
